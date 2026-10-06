#!/usr/bin/env python3
"""macOS Claude 窗口管理、真实请求历史与进程管理工具。"""

import argparse
from collections import deque
from datetime import datetime, time as clock_time, timedelta, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import resource
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

HOME = Path.home()
LABEL = "com.weicoz.claude-window"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
PREFS = HOME / "Library/Preferences/com.appshub.bettbox.plist"
CACHE = HOME / ".claude/rl-cache.json"
BASE = HOME / ".local/state/claude-window"
STATE = BASE / "state.json"
CONFIG = BASE / "config.json"
HISTORY = BASE / "requests.jsonl"
LOCK = BASE / "check.lock"
WORK = BASE / "empty-workdir"
LOG = HOME / "Library/Logs/claude-window.jsonl"
ERR = HOME / "Library/Logs/claude-window.launchd.err.log"
CLAUDE = HOME / ".local/bin/claude"
INTERVAL = 600
GRACE = 600
WINDOW = 5 * 3600
MAX_PER_DAY = 4
SCHEDULE_TOLERANCE = 20 * 60


def ensure_dirs():
    for p in (BASE, WORK, LOG.parent, PLIST.parent):
        p.mkdir(parents=True, exist_ok=True)
    BASE.chmod(0o700)
    WORK.chmod(0o700)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}


def write_json(path, data):
    ensure_dirs()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def log(event, **data):
    ensure_dirs()
    if LOG.exists() and LOG.stat().st_size > 5_000_000:
        LOG.replace(LOG.with_suffix(".jsonl.1"))
    record = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **data}
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def epoch(value):
    try:
        if isinstance(value, (int, float)):
            n = float(value)
        elif isinstance(value, str):
            try:
                n = float(value)
            except ValueError:
                n = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        else:
            return None
        if n > 100_000_000_000:
            n /= 1000
        return n if 1_600_000_000 < n < 4_000_000_000 else None
    except (ValueError, OverflowError):
        return None


def percent(value):
    try:
        n = float(value)
        return n if 0 <= n <= 100 else None
    except (TypeError, ValueError):
        return None


def local_time(value):
    return datetime.fromtimestamp(value).strftime("%m-%d %H:%M") if value else "未知"


def request_time(value):
    return datetime.fromtimestamp(value).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if value else "未知"


def history_events():
    if HISTORY.exists():
        with HISTORY.open() as f:
            for line in f:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        yield item
                except ValueError:
                    continue


def record_request(event, **data):
    ensure_dirs()
    record = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **data}
    fd = os.open(HISTORY, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())


def migrate_history():
    """只迁入旧日志能够证明的结果时间，不推算发起时间。调用方持有 LOCK。"""
    seen = {item.get("legacy_key") for item in history_events()}
    for path in (LOG.with_suffix(".jsonl.1"), LOG):
        if not path.exists():
            continue
        with path.open() as f:
            for line in f:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("event") not in ("sent", "failed"):
                    continue
                key = f"{item.get('event')}:{item.get('at')}"
                if key in seen:
                    continue
                record_request("legacy_" + item["event"], legacy_key=key,
                               finished_at=epoch(item.get("at")))
                seen.add(key)


def show_history(limit=20):
    records = {}
    for item in history_events():
        key = item.get("request_id") or item.get("legacy_key")
        if key:
            records.setdefault(key, {}).update(item)
    if not records:
        print("还没有真实请求历史")
        return
    for item in list(records.values())[-limit:]:
        started = item.get("started_at")
        event = item.get("event")
        label = {"request_started": "已发起，结果未记录", "sent": "成功", "failed": "失败",
                 "legacy_sent": "成功（旧记录，发起时间未知）",
                 "legacy_failed": "失败（旧记录，发起时间未知）"}.get(event, event)
        stamp = request_time(started or item.get("finished_at"))
        kind = "发起" if started else "完成"
        print(f"{kind} {stamp}；{label}")


def system_zone():
    target = os.path.realpath("/etc/localtime")
    return ZoneInfo(target.split("zoneinfo/", 1)[1])


def scheduled_due(q, state, now):
    if state.get("mode", "auto") in ("once", "daily"):
        plan = state.get("plan") or {}
        return plan.get("trigger_at") if plan.get("status") == "pending" else None
    if state.get("mode", "auto") == "auto":
        return due_time(q, state, now)
    return None


def advance_daily_plan(state, now, outcome):
    plan = state["plan"]
    zone = ZoneInfo(plan["timezone"])
    target = datetime.fromtimestamp(plan["target_reset_at"], zone)
    elapsed_targets = 0
    while target.timestamp() - WINDOW <= now + 60:
        target += timedelta(days=1)
        elapsed_targets += 1
    if isinstance(plan.get("remaining"), int):
        plan["remaining"] -= elapsed_targets
        if plan["remaining"] <= 0:
            plan.update(status="completed", last_outcome=outcome, last_outcome_at=now)
            return
    plan.update(status="pending", target_reset_at=target.timestamp(), trigger_at=target.timestamp() - WINDOW,
                last_outcome=outcome, last_outcome_at=now)


def quota(cache):
    limits = cache.get("rate_limits") or {}
    five = limits.get("five_hour") or {}
    week = limits.get("seven_day") or {}
    return {
        "observed": epoch(cache.get("observed_at")),
        "five_used": percent(five.get("used_percentage")),
        "five_reset": epoch(five.get("resets_at")),
        "week_used": percent(week.get("used_percentage")),
        "week_reset": epoch(week.get("resets_at")),
    }


def due_time(q, state, now):
    last = state.get("last_sent_at") or 0
    reset = q["five_reset"]
    if reset and not (now - 86400 <= reset <= now + WINDOW + 3600):
        reset = None
    if last and reset and reset > last:
        return reset + GRACE
    if last:
        return last + WINDOW + GRACE
    if reset and q["observed"] and now - q["observed"] < 86400:
        return reset + GRACE
    return None


def tun_ok():
    try:
        with PREFS.open("rb") as f:
            if not plistlib.load(f).get("flutter.is_tun_running"):
                return False, "bettbox_tun_off"
        route = subprocess.run(["/sbin/route", "-n", "get", "1.1.1.1"], capture_output=True, text=True, timeout=3)
        m = re.search(r"interface:\s*(utun\d+)", route.stdout)
        if route.returncode or not m:
            return False, "route_not_tun"
        iface = subprocess.run(["/sbin/ifconfig", m.group(1)], capture_output=True, text=True, timeout=3)
        if iface.returncode or not re.search(r"\binet 198\.18\.0\.1\b", iface.stdout):
            return False, "bettbox_tun_address_missing"
        return True, m.group(1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, "tun_check_failed"


def outlet_ok():
    # 使用 Claude API 的同一域名，确保验证对应域名的路由。
    expected = read_json(CONFIG).get("expected_ip")
    try:
        expected = str(ipaddress.IPv4Address(expected))
    except (ValueError, TypeError):
        return False, "expected_exit_not_configured"
    try:
        p = subprocess.run(
            ["/usr/bin/curl", "-4", "--noproxy", "*", "-fsS", "--connect-timeout", "3", "--max-time", "8",
             "https://api.anthropic.com/cdn-cgi/trace"],
            capture_output=True, text=True, timeout=10,
        )
        if p.returncode:
            return False, "anthropic_route_check_failed"
        match = re.search(r"(?m)^ip=([0-9.]+)\s*$", p.stdout)
        if not match:
            return False, "anthropic_route_ip_missing"
        ip = match.group(1)
        ipaddress.IPv4Address(ip)
        return (ip == expected), ip
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False, "anthropic_route_check_failed"


def auth_ok():
    try:
        p = subprocess.run([str(CLAUDE), "auth", "status"], capture_output=True, text=True, timeout=8)
        a = json.loads(p.stdout) if p.returncode == 0 else {}
        return a.get("loggedIn") is True and a.get("authMethod") == "claude.ai" and a.get("apiProvider") == "firstParty"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def send_minimal(state):
    env = os.environ.copy()
    for key in list(env):
        if key.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy") or key in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN"
        ):
            env.pop(key, None)
    command = [str(CLAUDE), "--safe-mode", "--no-chrome", "--no-session-persistence", "--tools", "",
               "-p", "--model", "haiku", "--output-format", "json", "Reply exactly OK."]
    start = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    proc = subprocess.Popen(command, cwd=WORK, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    started_at = time.time()
    request_id = f"{time.time_ns()}-{proc.pid}"
    try:
        record_request("request_started", request_id=request_id, started_at=started_at, pid=proc.pid)
        state["last_request_started_at"] = started_at
        write_json(STATE, state)
    except OSError:
        proc.kill()
        proc.communicate()
        raise
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=1)
            break
        except subprocess.TimeoutExpired:
            good, reason = tun_ok()
            if not good or time.monotonic() - start > 120:
                proc.terminate()
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                return False, {"request_id": request_id, "started_at": started_at,
                               "reason": "tun_lost" if not good else "timeout", "duration_s": round(time.monotonic()-start, 1)}
    detail = {"request_id": request_id, "started_at": started_at, "duration_s": round(time.monotonic()-start, 1),
              "peak_rss_mb": round(max(before, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)/1048576, 1)}
    try:
        result = json.loads(stdout)
        detail["model"] = next(iter(result.get("modelUsage", {})), "haiku")
        detail["estimated_api_cost_usd"] = result.get("total_cost_usd")
        detail["is_error"] = result.get("is_error", False)
        usage = result.get("usage") or {}
        detail["input_tokens"] = usage.get("input_tokens")
        detail["output_tokens"] = usage.get("output_tokens")
    except ValueError:
        detail["result"] = "non_json"
    if proc.returncode or detail.get("is_error") or detail.get("result") == "non_json":
        detail["reason"] = (stderr or stdout)[-400:]
        return False, detail
    return True, detail


def check(force=False, dry_run=False):
    ensure_dirs()
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有一次检查在运行")
            return 0
        state, cache = read_json(STATE), read_json(CACHE)
        q, now = quota(cache), time.time()
        mode = state.get("mode", "auto")
        scheduled = mode in ("once", "daily")
        plan = state.get("plan") or {}
        due = scheduled_due(q, state, now)
        sent = [x for x in state.get("sent_times", []) if now-x < 86400]
        state["sent_times"] = sent
        reason = None
        if scheduled and plan.get("status") == "pending" and not isinstance(due, (int, float)):
            reason = "invalid_scheduled_plan"
        elif scheduled and plan.get("status") != "pending":
            reason = "scheduled_plan_finished"
        elif scheduled and now < due:
            reason = "scheduled_wait"
        elif scheduled and now > due + SCHEDULE_TOLERANCE:
            if mode == "daily": advance_daily_plan(state, now, "missed_late")
            else: plan["status"] = "missed_late"
            reason = "scheduled_time_missed"
        elif scheduled and state.get("last_sent_at") and state["last_sent_at"] < now and state["last_sent_at"] + WINDOW > now:
            if mode == "daily": advance_daily_plan(state, now, "missed_active_window")
            else: plan["status"] = "missed_active_window"
            reason = "known_window_still_active"
        elif scheduled and q["observed"] and q["observed"] >= due - 1800 and q["five_reset"] and q["five_reset"] > now + 60:
            reason = "satisfied_by_other_usage" if abs(q["five_reset"] - plan["target_reset_at"]) <= SCHEDULE_TOLERANCE else "missed_active_window"
            if mode == "daily": advance_daily_plan(state, now, reason)
            else: plan["status"] = reason
        elif mode == "paused" and not force:
            reason = "paused"
        elif not force and due is None:
            reason = "waiting_for_fresh_statusline_cache"
        elif not force and now < due:
            reason = "not_due"
        elif len(sent) >= MAX_PER_DAY:
            reason = "daily_cap_4"
        elif (state.get("retry_after") or 0) > now:
            reason = "error_backoff"
        elif q["week_used"] is not None and q["week_used"] >= 95 and (q["week_reset"] or now+1) > now:
            reason = "weekly_usage_above_95_percent"
        if reason:
            if mode == "daily": due = scheduled_due(q, state, now)
            log("skip", reason=reason, next_due=due)
            state["last_check_at"], state["last_event"] = now, reason
            write_json(STATE, state)
            if dry_run:
                print(f"跳过：{reason}；下次最早 {local_time(due)}")
            return 0
        tun, tun_detail = tun_ok()
        if not tun:
            reason = tun_detail
        else:
            outlet, outlet_detail = outlet_ok()
            if not outlet:
                reason = f"wrong_or_unreachable_exit:{outlet_detail}"
            elif not auth_ok():
                reason = "subscription_oauth_not_active"
        if reason:
            log("skip", reason=reason, next_due=due)
            state["last_check_at"], state["last_event"] = now, reason
            write_json(STATE, state)
            if dry_run:
                print(f"跳过：{reason}")
            return 0
        if dry_run:
            print(f"可发送：TUN={tun_detail}，出口={outlet_detail}，下次最早={local_time(due)}；本次未调用 Claude")
            return 0
        ok, detail = send_minimal(state)
        state["last_check_at"] = time.time()
        state["last_result"] = detail
        if ok:
            state["last_sent_at"] = time.time()
            state["sent_times"].append(state["last_sent_at"])
            state["last_event"] = "sent"
            planned_reset = plan.get("target_reset_at") if scheduled else None
            if mode == "once":
                plan["status"] = "completed"
                plan["sent_at"] = state["last_sent_at"]
            elif mode == "daily":
                advance_daily_plan(state, state["last_sent_at"], "sent")
            state.pop("retry_after", None)
            log("sent", planned_reset=planned_reset, **detail)
            print("已发送 Haiku 最小请求")
        else:
            state["last_event"] = "failed"
            state["retry_after"] = time.time() + 3600
            log("failed", **detail)
            print(f"请求失败：{detail.get('reason', 'unknown')}")
        write_json(STATE, state)
        record_request("sent" if ok else "failed", finished_at=state["last_check_at"],
                       request_id=detail["request_id"], started_at=detail["started_at"])
        return 0 if ok else 1


def show_usage():
    cache, state = read_json(CACHE), read_json(STATE)
    q, now = quota(cache), time.time()
    age = f"{(now-q['observed'])/60:.0f} 分钟前" if q["observed"] else "无缓存"
    print(f"额度缓存：{age}（仅在 Claude Code 状态栏刷新时更新）")
    for label, used, reset in (("5 小时", q["five_used"], q["five_reset"]), ("7 天", q["week_used"], q["week_reset"])):
        value = f"已用 {used:.1f}% / 剩余 {100-used:.1f}%" if used is not None else "未知"
        print(f"{label}：{value}；重置 {local_time(reset)}")
    print(f"下次计划发出：{local_time(scheduled_due(q, state, now))}")
    started = state.get("last_request_started_at")
    for item in history_events():
        started = max(started or 0, item.get("started_at") or 0)
    print(f"上次真实发起：{request_time(started)}（工具启动 Claude 请求；不含跳过/模拟检查）")
    print(f"上次成功完成：{request_time(state.get('last_sent_at'))}；近 24 小时 {len([x for x in state.get('sent_times', []) if now-x < 86400])}/{MAX_PER_DAY} 次")


def loaded():
    p = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    return p.returncode == 0, p.stdout


def show_status():
    active, output = loaded()
    print(f"定时任务：{'已启动' if active else '已停止'}；每 {INTERVAL//60} 分钟检查一次；未运行时常驻内存 0")
    m = re.search(r"\bpid = (\d+)", output) if active else None
    if m:
        p = subprocess.run(["/bin/ps", "-p", m.group(1), "-o", "pid=,%cpu=,rss=,etime="], capture_output=True, text=True)
        print("当前进程 PID/%CPU/RSS KiB/运行时长：", p.stdout.strip() or "无")
    state = read_json(STATE)
    mode = state.get("mode", "auto")
    if mode in ("once", "daily"):
        plan = state.get("plan") or {}
        label = (f"重复计划（剩余 {plan['remaining']} 次）" if isinstance(plan.get("remaining"), int)
                 else "每天重复" if mode == "daily" else "一次性")
        print(f"模式：{label}；计划状态：{plan.get('status', '未知')}；预计重置 {local_time(plan.get('target_reset_at'))}；计划发送 {local_time(plan.get('trigger_at'))}")
    else:
        print(f"模式：{'周期自动' if mode == 'auto' else '暂停'}")
    print(f"最近检查：{local_time(state.get('last_check_at'))}；结果：{state.get('last_event', '无')}")
    result = state.get("last_result") or {}
    if result:
        print(f"上次请求耗时：{result.get('duration_s', '未知')} 秒；峰值子进程 RSS：{result.get('peak_rss_mb', '未知')} MB")
    show_usage()
    print(f"执行日志：{LOG}")


def start():
    ensure_dirs()
    if not PLIST.exists():
        print(f"缺少 LaunchAgent：{PLIST}", file=sys.stderr)
        return 1
    if loaded()[0]:
        print("定时任务已经启动")
        return 0
    subprocess.run(["/bin/launchctl", "enable", f"gui/{os.getuid()}/{LABEL}"], check=True)
    subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True)
    print("定时任务已启动；只有 TUN 和配置的固定出口同时通过检查才会发请求")
    return 0


def stop():
    if loaded()[0]:
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=True)
    subprocess.run(["/bin/launchctl", "disable", f"gui/{os.getuid()}/{LABEL}"], check=True)
    print("定时任务已停止，后续登录也保持禁用，直到再次运行 start")
    return 0


def schedule_once(day, clock):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
        raise ValueError("时间请写 HH:MM，例如 10:30")
    tz = system_zone()
    today = datetime.now(tz).date()
    if day == "tomorrow":
        target_day = today + timedelta(days=1)
    elif day == "today":
        target_day = today
    else:
        target_day = datetime.strptime(day, "%Y-%m-%d").date()
    hour, minute = map(int, clock.split(":"))
    target = datetime.combine(target_day, clock_time(hour, minute), tzinfo=tz)
    return save_schedule(target, "once")


def schedule_clock(clock, daily=False, times=None):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock):
        raise ValueError("时间请写 HH:MM，例如 10:30")
    tz = system_zone()
    now = datetime.now(tz)
    if times is not None and not 1 <= times <= 365:
        raise ValueError("重复次数须在 1 到 365 之间")
    mode = "daily" if daily or (times is not None and times > 1) else "once"
    hour, minute = map(int, clock.split(":"))
    for days in range(3):
        target = datetime.combine(now.date() + timedelta(days=days), clock_time(hour, minute), tzinfo=tz)
        if target.timestamp() - WINDOW > now.timestamp() + 60:
            return save_schedule(target, mode, remaining=times if times and times > 1 else None)
    raise ValueError("找不到未来可执行的发送时间")


def save_schedule(target, mode, remaining=None):
    trigger = target.timestamp() - WINDOW
    if trigger <= time.time() + 60:
        raise ValueError("反推后的发送时间已过去或不足 1 分钟；请选更晚的重置时间")
    ensure_dirs()
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(STATE)
        state["mode"] = mode
        state["plan"] = {"status": "pending", "target_reset_at": target.timestamp(), "trigger_at": trigger, "timezone": str(target.tzinfo)}
        if remaining is not None:
            state["plan"]["remaining"] = remaining
        state.pop("retry_after", None)
        write_json(STATE, state)
    log("scheduled_" + mode, target_reset=target.timestamp(), trigger=trigger, remaining=remaining)
    label = f"重复 {remaining} 次" if remaining is not None else "每天重复" if mode == "daily" else "一次性"
    print(f"{label}计划：{local_time(trigger)} 左右发送；预计 {local_time(target.timestamp())} 重置（{target.tzinfo}）")
    print(f"错过发送时间超过 {SCHEDULE_TOLERANCE//60} 分钟或当时已有活跃窗口，就不补发")
    return start()


def cancel_schedule():
    ensure_dirs()
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(STATE)
        if state.get("mode") in ("once", "daily") and (state.get("plan") or {}).get("status") == "pending":
            state["plan"]["status"] = "cancelled"
        state["mode"] = "paused"
        write_json(STATE, state)
    log("schedule_cancelled")
    return stop()


def resume_auto():
    ensure_dirs()
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(STATE)
        state["mode"] = "auto"
        state.pop("plan", None)
        write_json(STATE, state)
    log("auto_resumed")
    return start()


def kill_claude(dry_run=False):
    """保留 ckill 的完整命令行匹配范围，排除当前用户以外及祖先进程。"""
    output = subprocess.run(["/bin/ps", "-axo", "pid=,ppid=,uid=,command="],
                            capture_output=True, text=True, check=True).stdout
    processes = {}
    for line in output.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) == 4:
            pid, parent, uid = map(int, fields[:3])
            processes[pid] = (parent, uid, fields[3])
    excluded = set()
    pid = os.getpid()
    while pid and pid not in excluded:
        excluded.add(pid)
        pid = processes.get(pid, (0, 0, ""))[0]
    targets = [(pid, command) for pid, (_, uid, command) in processes.items()
               if uid == os.getuid() and pid not in excluded and "claude" in command.lower()]
    if not targets:
        print("没有匹配的 Claude 进程")
        return 0
    print("强制终止范围：当前用户命令行含 claude 的进程；未保存内容可能丢失。")
    failed = False
    for pid, command in targets:
        if dry_run:
            print(f"PID {pid}：{command}")
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"已终止 PID {pid}")
        except ProcessLookupError:
            pass
        except PermissionError:
            failed = True
            print(f"无权终止 PID {pid}", file=sys.stderr)
    if dry_run:
        print("仅预览，未终止任何进程。")
    return int(failed)


def main():
    if len(sys.argv) > 1 and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", sys.argv[1]):
        quick = argparse.ArgumentParser(prog="claude-w", description="直接按目标重置时间安排；自动反推 5 小时")
        quick.add_argument("time", help="目标重置时间 HH:MM（下一次可执行的日期）")
        repetition = quick.add_mutually_exclusive_group()
        repetition.add_argument("-d", "--daily", action="store_true", help="每天重复，直到 cancel")
        repetition.add_argument("-n", "--times", type=int, help="连续安排 N 个目标日期")
        args = quick.parse_args()
        return schedule_clock(args.time, daily=args.daily, times=args.times)
    parser = argparse.ArgumentParser(
        prog="claude-w",
        description="Claude 5 小时窗口监控；代理未就绪时不发请求",
        epilog="快捷设置：claude-w 10:00（一次）；claude-w 10:00 -n 3（三次）；claude-w 10:00 -d（每天）",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "stop", "status", "usage", "logs", "poke", "cancel", "auto"):
        sub.add_parser(name)
    history = sub.add_parser("history", help="显示真实请求历史，区分发起与完成时间")
    history.add_argument("--limit", type=int, default=20, help="显示最近 N 次请求，默认 20")
    kill = sub.add_parser("kill", aliases=["ckill"], help="强制终止 Claude 相关进程，可能丢失未保存内容")
    kill.add_argument("--dry-run", action="store_true", help="仅列出进程，不终止")
    config = sub.add_parser("config", help="配置固定出口 IPv4；未配置时不会发送请求")
    config.add_argument("--expected-ip", type=ipaddress.IPv4Address, required=True)
    schedule = sub.add_parser("schedule", help="按目标重置时间创建一次性计划并启动任务")
    schedule.add_argument("day", help="tomorrow、today 或 YYYY-MM-DD")
    schedule.add_argument("time", help="目标重置时间 HH:MM（本机时区）")
    test = sub.add_parser("check")
    test.add_argument("--dry-run", action="store_true")
    test.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.command in ("kill", "ckill"): return kill_claude(dry_run=args.dry_run)
    if args.command == "history":
        if args.limit < 1: parser.error("--limit 必须大于 0")
        show_history(args.limit)
        return 0
    if args.command == "config":
        write_json(CONFIG, {"expected_ip": str(args.expected_ip)})
        print("固定出口已配置；下次检查生效")
        return 0
    if args.command == "start": return start()
    if args.command == "stop": return stop()
    if args.command == "schedule": return schedule_once(args.day, args.time)
    if args.command == "cancel": return cancel_schedule()
    if args.command == "auto": return resume_auto()
    if args.command == "status": show_status(); return 0
    if args.command == "usage": show_usage(); return 0
    if args.command == "logs":
        if not LOG.exists(): print("还没有日志"); return 0
        for line in deque(LOG.open(), maxlen=20): print(line.rstrip())
        return 0
    if args.command == "poke": return check(force=True)
    return check(force=args.force, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"任务错误：{exc}", file=sys.stderr)
        sys.exit(1)
