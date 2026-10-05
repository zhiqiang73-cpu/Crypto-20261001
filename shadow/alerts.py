"""外部告警通道 —— 让「没人盯着」的时候也能知道出事了。

为什么需要这个模块
------------------
2026-10-04 验收评估结论：系统在交易逻辑层做得扎实，但**出事时没人会知道**。
唯一的通知机制是 `utils/alert_sound.play_alert()` —— 它让**本机**发出声音，
而无人值守的定义就是「没人在机器旁边」。

设计原则（每一条都对应一个真实故障场景）
--------------------------------------
1. **绝不阻塞交易主循环。** 告警通道故障（网络断、webhook 挂了）不能拖慢
   15 秒一轮的主循环。远端发送走后台守护线程，主线程只做本地落盘。
2. **本地落盘先于远端发送。** 无论通道是否配置、是否成功，`alerts.jsonl`
   一定要有一条。否则「告警系统自己坏了」会变成静默故障。
3. **绝不抛异常。** 告警失败不能把交易主循环带崩 —— 那是用次要故障制造主要
   故障。所有通道各自 try/except。
4. **同键冷却。** 崩溃循环里同一个错误每 10 秒报一次会刷爆通道，也会淹没
   真问题（实测 panel 崩溃循环刷了 2775 行）。同 key 在冷却期内只发一次，
   但**落盘不冷却** —— 记录要完整。
5. **未配置通道时不静默。** 第一次调用会打印一行提示，明确告诉运维
   「告警只落了本地盘，没人会被通知」。静默的告警等于没有告警。

配置（全部可选，未配置则该通道禁用）
----------------------------------
    CRYPTO_ALERT_WEBHOOK   通用 webhook，POST JSON
    CRYPTO_ALERT_BARK      Bark（iOS 推送）地址，如 https://api.day.app/xxxx
    CRYPTO_ALERT_OSASCRIPT "1" 时额外发 macOS 本地通知（默认关）
    CRYPTO_ALERT_COOLDOWN  同键冷却秒数，默认 300
    CRYPTO_ALERT_TIMEOUT   远端超时秒数，默认 5
    CRYPTO_ALERT_DISABLE   "1" 时全部禁用（含落盘），仅测试用
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

__all__ = ["notify", "alert_log_path", "ALERT_LEVELS"]

ALERT_LEVELS = ("INFO", "WARNING", "CRITICAL")

# 默认落盘位置：<repo>/runtime/shadow/alerts.jsonl
# 用 __file__ 推导而不是 import deploy 的常量，避免循环导入。
_DEFAULT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "runtime", "shadow"))
ALERT_LOG = os.path.join(_DEFAULT_DIR, "alerts.jsonl")

_COOLDOWN_SEC = float(os.environ.get("CRYPTO_ALERT_COOLDOWN", "300") or 300)
_TIMEOUT_SEC = float(os.environ.get("CRYPTO_ALERT_TIMEOUT", "5") or 5)
_DISABLED = os.environ.get("CRYPTO_ALERT_DISABLE", "") == "1"

_last_sent: Dict[str, float] = {}
_lock = threading.Lock()
_warned_unconfigured = False


def alert_log_path() -> str:
    """告警落盘路径（供测试与运维核对）。"""
    return ALERT_LOG


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _append_local(payload: Dict[str, Any]) -> None:
    """本地落盘。**不参与冷却** —— 记录必须完整。"""
    try:
        os.makedirs(os.path.dirname(ALERT_LOG) or ".", exist_ok=True)
        with open(ALERT_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 告警失败绝不能影响主循环
        print(f"[告警] 本地落盘失败: {type(exc).__name__}: {exc}")


def _post_json(url: str, body: Dict[str, Any]) -> None:
    import urllib.request

    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
        resp.read(256)


def _post_bark(base: str, title: str, detail: str) -> None:
    """Bark 用 GET 路径传参，不需要 JSON body。"""
    import urllib.parse
    import urllib.request

    url = base.rstrip("/") + "/" + urllib.parse.quote(title) + "/" + \
        urllib.parse.quote(detail[:400])
    with urllib.request.urlopen(url, timeout=_TIMEOUT_SEC) as resp:
        resp.read(256)


def _send_remote(payload: Dict[str, Any]) -> None:
    """远端发送。在后台线程里跑，失败只打印。"""
    title = f"[{payload['level']}] {payload['title']}"
    detail = payload.get("detail") or ""
    hook = (os.environ.get("CRYPTO_ALERT_WEBHOOK") or "").strip()
    bark = (os.environ.get("CRYPTO_ALERT_BARK") or "").strip()
    if hook:
        try:
            _post_json(hook, payload)
        except Exception as exc:  # noqa: BLE001
            print(f"[告警] webhook 失败: {type(exc).__name__}: {exc}")
    if bark:
        try:
            _post_bark(bark, title, detail)
        except Exception as exc:  # noqa: BLE001
            print(f"[告警] bark 失败: {type(exc).__name__}: {exc}")
    if os.environ.get("CRYPTO_ALERT_OSASCRIPT", "") == "1":
        try:
            import subprocess
            script = (f'display notification {json.dumps(detail[:200])} '
                      f'with title {json.dumps(title)}')
            subprocess.run(["osascript", "-e", script], timeout=_TIMEOUT_SEC,
                           check=False, capture_output=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[告警] osascript 失败: {type(exc).__name__}: {exc}")


def notify(level: str, key: str, title: str,
           detail: str = "", **extra: Any) -> bool:
    """发一条告警。返回是否**实际发出**（冷却/禁用时为 False）。

    参数
    ----
    level  : INFO / WARNING / CRITICAL（非法值降级为 WARNING，不报错）
    key    : 去重键，同一 key 在冷却期内只发一次远端
    title  : 一句话说明
    detail : 细节（含数值、路径、原因）
    extra  : 附加字段，原样进入落盘与 webhook

    绝不抛异常，绝不阻塞。
    """
    global _warned_unconfigured
    if _DISABLED:
        return False
    lvl = level if level in ALERT_LEVELS else "WARNING"
    payload: Dict[str, Any] = {
        "ts": _now_iso(),
        "epoch": time.time(),
        "level": lvl,
        "key": key,
        "title": title,
        "detail": detail,
        "pid": os.getpid(),
    }
    payload.update(extra)

    _append_local(payload)

    # 本地可见：即使没有任何通道配置，stdout 也要能看见（有标记便于 grep）
    print(f"[告警/{lvl}] {title}" + (f" — {detail}" if detail else ""))

    now = time.time()
    with _lock:
        last = float(_last_sent.get(key) or 0.0)
        if now - last < _COOLDOWN_SEC:
            return False
        _last_sent[key] = now
        unconfigured = not (
            (os.environ.get("CRYPTO_ALERT_WEBHOOK") or "").strip()
            or (os.environ.get("CRYPTO_ALERT_BARK") or "").strip()
            or os.environ.get("CRYPTO_ALERT_OSASCRIPT", "") == "1")
        first_warn = unconfigured and not _warned_unconfigured
        if first_warn:
            _warned_unconfigured = True

    if first_warn:
        print("[告警] ⚠ 未配置任何外部通道（CRYPTO_ALERT_WEBHOOK / "
              "CRYPTO_ALERT_BARK），告警只落在 alerts.jsonl —— "
              "无人值守时不会有任何人被通知。")

    threading.Thread(target=_send_remote, args=(payload,), daemon=True).start()
    return True


def reset_cooldown() -> None:
    """清空冷却表（测试用）。"""
    with _lock:
        _last_sent.clear()
