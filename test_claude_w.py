"""隔离验证请求时间、旧历史、出口保护、安装和进程终止；无真实外部副作用。"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import claude_w as app
import install as installer


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        base = home / ".local/state/claude-window"
        paths = {"HOME": home, "BASE": base, "WORK": base / "empty-workdir",
                 "STATE": base / "state.json", "CONFIG": base / "config.json",
                 "HISTORY": base / "requests.jsonl", "LOCK": base / "check.lock",
                 "LOG": home / "Library/Logs/claude-window.jsonl",
                 "PLIST": home / "Library/LaunchAgents/com.weicoz.claude-window.plist",
                 "ERR": home / "Library/Logs/claude-window.err.log",
                 "CACHE": home / ".claude/rl-cache.json", "CLAUDE": home / "bin/claude"}
        self.patcher = patch.multiple(app, **paths)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        app.ensure_dirs()

    def invoke_check(self, success=True):
        proc = Mock(pid=12345, returncode=0 if success else 1)

        def reply(**kwargs):
            events = list(app.history_events())
            self.assertEqual(events[0]["event"], "request_started")
            self.assertGreater(app.read_json(app.STATE)["last_request_started_at"], 0)
            return json.dumps({"is_error": not success, "usage": {}}), ""

        proc.communicate.side_effect = reply
        with patch.object(app, "tun_ok", return_value=(True, "utun0")), \
             patch.object(app, "outlet_ok", return_value=(True, "test-exit")), \
             patch.object(app, "auth_ok", return_value=True), \
             patch.object(app.subprocess, "Popen", return_value=proc), \
             redirect_stdout(io.StringIO()):
            code = app.check(force=True)
        return code, list(app.history_events()), app.read_json(app.STATE)

    def test_real_start_recorded_before_response_and_status(self):
        code, events, state = self.invoke_check()
        self.assertEqual(code, 0)
        self.assertEqual([e["event"] for e in events], ["request_started", "sent"])
        self.assertEqual(events[0]["started_at"], state["last_request_started_at"])
        self.assertEqual(events[0]["request_id"], events[1]["request_id"])
        self.assertLessEqual(events[0]["started_at"], state["last_sent_at"])
        self.assertEqual(app.HISTORY.stat().st_mode & 0o777, 0o600)
        output = io.StringIO()
        with patch.object(app, "loaded", return_value=(False, "")), redirect_stdout(output):
            app.show_status()
        self.assertIn("上次真实发起：" + app.request_time(state["last_request_started_at"]), output.getvalue())

    def test_failed_request_has_start_but_no_success_timestamp(self):
        code, events, state = self.invoke_check(success=False)
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["event"], "failed")
        self.assertGreater(state["last_request_started_at"], 0)
        self.assertNotIn("last_sent_at", state)

    def test_skipped_and_dry_run_do_not_create_request_history(self):
        with patch.object(app.subprocess, "Popen") as popen, redirect_stdout(io.StringIO()):
            app.check()
            with patch.object(app, "tun_ok", return_value=(True, "utun0")), \
                 patch.object(app, "outlet_ok", return_value=(True, "test-exit")), \
                 patch.object(app, "auth_ok", return_value=True):
                app.check(force=True, dry_run=True)
        popen.assert_not_called()
        self.assertFalse(app.HISTORY.exists())
        self.assertNotIn("last_request_started_at", app.read_json(app.STATE))

    def test_launch_failure_does_not_forge_start(self):
        with patch.object(app.subprocess, "Popen", side_effect=OSError("模拟无法启动")):
            with self.assertRaises(OSError):
                app.send_minimal({})
        self.assertFalse(app.HISTORY.exists())

    def test_result_history_failure_keeps_success_state(self):
        record = app.record_request

        def fail_result(event, **data):
            if event == "sent":
                raise OSError("模拟历史文件无法写入")
            return record(event, **data)

        with patch.object(app, "record_request", side_effect=fail_result):
            with self.assertRaises(OSError):
                self.invoke_check()
        self.assertGreater(app.read_json(app.STATE)["last_sent_at"], 0)

    def test_legacy_history_migration_is_idempotent_and_honest(self):
        app.LOG.write_text(json.dumps({"event": "sent", "at": "2026-10-01T00:00:00+00:00",
                                       "duration_s": 8, "private_text": "不迁入"}) + "\n")
        app.migrate_history()
        app.migrate_history()
        events = list(app.history_events())
        self.assertEqual(len(events), 1)
        self.assertNotIn("started_at", events[0])
        self.assertNotIn("private_text", events[0])
        output = io.StringIO()
        with redirect_stdout(output):
            app.show_history()
            app.show_usage()
        self.assertIn("发起时间未知", output.getvalue())
        self.assertIn("上次真实发起：未知", output.getvalue())

    def test_pending_start_survives_without_result(self):
        app.record_request("request_started", request_id="pending", started_at=1790812800)
        output = io.StringIO()
        with redirect_stdout(output):
            app.show_history()
        self.assertIn("已发起，结果未记录", output.getvalue())

    def test_missing_or_wrong_exit_never_launches_claude(self):
        with patch.object(app.subprocess, "run") as run:
            self.assertEqual(app.outlet_ok(), (False, "expected_exit_not_configured"))
            run.assert_not_called()
        app.write_json(app.CONFIG, {"expected_ip": "203.0.113.1"})
        with patch.object(app.subprocess, "run", return_value=Mock(returncode=0, stdout="ip=203.0.113.2\n")):
            self.assertFalse(app.outlet_ok()[0])
        with patch.object(app, "tun_ok", return_value=(True, "utun0")), \
             patch.object(app, "outlet_ok", return_value=(False, "wrong-exit")), \
             patch.object(app.subprocess, "Popen") as popen:
            app.check(force=True)
        popen.assert_not_called()
        self.assertFalse(app.HISTORY.exists())

    def test_kill_preview_and_execution_share_safe_targets(self):
        table = ("100 90 501 python claude-w kill\n90 1 501 shell claude-w kill\n"
                 "200 1 501 /bin/claude\n201 1 502 /bin/claude\n202 1 501 /bin/codex\n")
        with patch.object(app.os, "getpid", return_value=100), \
             patch.object(app.os, "getuid", return_value=501), \
             patch.object(app.subprocess, "run", return_value=Mock(stdout=table)), \
             patch.object(app.os, "kill") as kill:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(app.kill_claude(dry_run=True), 0)
            kill.assert_not_called()
            self.assertIn("PID 200", output.getvalue())
            self.assertNotIn("PID 100", output.getvalue())
            self.assertNotIn("PID 90", output.getvalue())
            self.assertNotIn("PID 201", output.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(app.kill_claude(), 0)
            kill.assert_called_once_with(200, app.signal.SIGKILL)

    def test_install_preserves_state_plist_and_old_exit(self):
        bindir = app.HOME / ".local/bin"
        bindir.mkdir(parents=True)
        (bindir / "claude-window").write_text('EXPECTED_IP = "203.0.113.1"\n')
        app.PLIST.write_bytes("已有任务配置".encode())
        app.write_json(app.STATE, {"mode": "daily", "plan": {"status": "pending"}})
        state_before = app.STATE.read_bytes()
        with redirect_stdout(io.StringIO()):
            installer.install()
        self.assertEqual(app.STATE.read_bytes(), state_before)
        self.assertEqual(app.PLIST.read_bytes(), "已有任务配置".encode())
        self.assertEqual(app.read_json(app.CONFIG)["expected_ip"], "203.0.113.1")
        self.assertTrue((bindir / "claude-w").is_symlink())
        self.assertEqual((bindir / "claude-window").resolve(), (bindir / "claude-w").resolve())
        self.assertIn('kill "$@"', (bindir / "ckill").read_text())

    def test_shortcut_and_kill_alias_route_without_external_calls(self):
        with patch.object(app.sys, "argv", ["claude-w", "10:00", "-n", "3"]), \
             patch.object(app, "schedule_clock", return_value=0) as schedule:
            self.assertEqual(app.main(), 0)
        schedule.assert_called_once_with("10:00", daily=False, times=3)
        for command in ("kill", "ckill"):
            with patch.object(app.sys, "argv", ["claude-w", command, "--dry-run"]), \
                 patch.object(app, "kill_claude", return_value=0) as kill:
                self.assertEqual(app.main(), 0)
            kill.assert_called_once_with(dry_run=True)


if __name__ == "__main__":
    unittest.main()
