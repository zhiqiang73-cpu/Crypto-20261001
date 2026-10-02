"""重启测试网运行器：优雅停掉现有的 shadow.deploy，再起一份新的。

为什么需要它：运行器把策略代码在启动时读进内存，**改完策略必须重启才生效**。
手工重启容易踩两个坑 —— 旧进程没停干净就起新的（两个运行器抢同一个账户），
或者新进程起来后其实没连上。本脚本把「停 → 确认停干净 → 起 → 确认起来了」
做成一条可审计的链路。

用法：
    python3 -m scripts.restart_testnet_runner --dry-run   # 只看会做什么
    python3 -m scripts.restart_testnet_runner             # 真正重启

安全边界：只操作 `python -m shadow.deploy` 进程；只允许测试网（沿用 deploy.py
自身的 runtime_mode 闸门，主网依旧被拒）。不加 `--execute` 时不会下单。
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG = ROOT / "runtime" / "logs" / "testnet-auto-runner.log"
PIDFILE = ROOT / "runtime" / "logs" / "testnet-auto-runner.pid"
HEARTBEAT = ROOT / "runtime" / "shadow" / "runner_heartbeat.json"
PATTERN = r"shadow\.deploy"
STOP_TIMEOUT_SEC = 20
STARTUP_GRACE_SEC = 12


def runner_processes() -> list[tuple[int, str]]:
    """返回 [(pid, 命令行)]，只含 `python -m shadow.deploy`。"""
    try:
        out = subprocess.check_output(["pgrep", "-fl", PATTERN], text=True)
    except subprocess.CalledProcessError:
        return []
    found: list[tuple[int, str]] = []
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1]
        # 排除本脚本自己（pgrep -f 会匹配到写在同一命令里的模式串）。
        if "restart_testnet_runner" in cmd:
            continue
        try:
            argv = shlex.split(cmd)
        except ValueError:
            continue
        if "-m" in argv and argv[argv.index("-m") + 1] == "shadow.deploy":
            found.append((pid, cmd))
    return found


def stop_all(procs: list[tuple[int, str]]) -> None:
    for pid, cmd in procs:
        print(f"[停止] pid={pid} {cmd[:110]}")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            print(f"       pid={pid} 已经不在了")
    deadline = time.time() + STOP_TIMEOUT_SEC
    while time.time() < deadline:
        alive = [(p, c) for p, c in procs if _alive(p)]
        if not alive:
            print(f"[停止] 全部退出（{len(procs)} 个）")
            return
        time.sleep(0.5)
    still = [p for p, _ in procs if _alive(p)]
    raise SystemExit(
        f"拒绝继续：{still} 在 {STOP_TIMEOUT_SEC}s 内没有退出。"
        "先人工确认这些进程在做什么，再手动处理；不自动 SIGKILL。"
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def start(execute: bool) -> int:
    env = os.environ.copy()
    env.update(
        TRADING_MODE="testnet",
        PYTHONPATH=str(ROOT),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
    )
    argv = [sys.executable, "-B", "-u", "-m", "shadow.deploy",
            "--interval", "15"]
    if execute:
        argv.insert(-2, "--execute")
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("ab", buffering=0) as out:
        out.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 重启 "
                  f"{'（真实下单）' if execute else '（仅观察）'} =====\n".encode())
        proc = subprocess.Popen(
            argv, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
            stdout=out, stderr=out, start_new_session=True,
        )
    PIDFILE.write_text(f"{proc.pid}\n")
    return proc.pid


def heartbeat_age() -> float | None:
    try:
        data = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
        return time.time() - float(data["updated_ms"]) / 1000.0
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告，不动手")
    ap.add_argument("--observe", action="store_true",
                    help="新进程不加 --execute（只观察，不下单）")
    args = ap.parse_args()
    execute = not args.observe

    procs = runner_processes()
    print(f"[扫描] 找到 {len(procs)} 个 shadow.deploy 进程")
    for pid, cmd in procs:
        print(f"       pid={pid} {cmd[:110]}")
    if args.dry_run:
        print("[dry-run] 未做任何改动")
        return 0

    if procs:
        stop_all(procs)
    else:
        print("[停止] 没有在跑的运行器，直接启动")

    leftover = runner_processes()
    if leftover:
        raise SystemExit(f"拒绝启动：仍有 {leftover}，会与旧进程抢账户。")

    before = heartbeat_age()
    pid = start(execute)
    print(f"[启动] 新 pid={pid}，日志 {LOG}")

    deadline = time.time() + STARTUP_GRACE_SEC
    while time.time() < deadline:
        time.sleep(1.0)
        if not _alive(pid):
            raise SystemExit(
                f"新进程 pid={pid} 已退出；看日志 {LOG}（常见原因：账户有在途"
                "委托、密钥缺失、行情腿与下单腿不一致）。"
            )
        age = heartbeat_age()
        if age is not None and age < 20 and (before is None or age <= before + 1):
            print(f"[确认] 运行器在跑，心跳 {age:.1f}s 前更新")
            break
    else:
        print(f"[警告] {STARTUP_GRACE_SEC}s 内没看到心跳刷新，请查看 {LOG}")

    print(json.dumps({"pid": pid, "execute": execute, "log": str(LOG),
                      "stopped": [p for p, _ in procs]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
