#!/usr/bin/env python3
"""安装本地命令，备份已有文件；不启动任务、不调用 Claude。"""

import argparse
from datetime import datetime
import fcntl
import ipaddress
import os
from pathlib import Path
import plistlib
import re
import shutil
import sys

import claude_w as app


def install(expected_ip=None):
    source = Path(__file__).resolve().with_name("claude_w.py")
    bindir = app.HOME / ".local/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    app.ensure_dirs()
    with app.LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        backup = app.BASE / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup.mkdir(parents=True, mode=0o700)
        for path in (bindir / "claude-w", bindir / "claude-window", bindir / "ckill",
                     app.PLIST, app.STATE, app.CONFIG, app.HISTORY):
            if path.is_symlink():
                (backup / path.name).symlink_to(os.readlink(path))
            elif path.exists():
                shutil.copy2(path, backup / path.name)
        if expected_ip is None and not app.CONFIG.exists():
            old = bindir / "claude-window"
            if old.exists():
                match = re.search(r'^EXPECTED_IP = "([0-9.]+)"$', old.read_text(), re.M)
                if match:
                    expected_ip = ipaddress.IPv4Address(match.group(1))
        if expected_ip is not None:
            app.write_json(app.CONFIG, {"expected_ip": str(expected_ip)})
        app.migrate_history()
        source.chmod(0o755)
        for name, target in (("claude-w", source), ("claude-window", "claude-w")):
            path = bindir / name
            temp = path.with_name(f".{name}.{os.getpid()}.tmp")
            temp.symlink_to(target)
            temp.replace(path)
        path = bindir / "ckill"
        temp = path.with_name(f".ckill.{os.getpid()}.tmp")
        temp.write_text('#!/bin/sh\nexec "$HOME/.local/bin/claude-w" kill "$@"\n')
        temp.chmod(0o755)
        temp.replace(path)
        if not app.PLIST.exists():
            config = {
                "Label": app.LABEL,
                "ProgramArguments": [sys.executable, str(bindir / "claude-w"), "check"],
                "StartInterval": app.INTERVAL,
                "RunAtLoad": True,
                "ProcessType": "Background",
                "Nice": 10,
                "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
                "StandardOutPath": "/dev/null",
                "StandardErrorPath": str(app.ERR),
            }
            with app.PLIST.open("wb") as f:
                plistlib.dump(config, f)
        print(f"已安装 claude-w；ckill 和 claude-window 保留兼容。备份：{backup}")
        print("未启动/重启任务，也未调用 Claude；原已启用的任务继续使用新版本。")
        if not app.CONFIG.exists():
            print("请先运行 claude-w config --expected-ip <固定出口IPv4>，再运行 claude-w start。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-ip", type=ipaddress.IPv4Address)
    args = parser.parse_args()
    install(args.expected_ip)
