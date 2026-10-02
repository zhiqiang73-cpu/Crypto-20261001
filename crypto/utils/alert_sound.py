"""本机提示音。失败不得影响下单。

委托提交：短促一声。开仓成交：更清楚的一声。
单测 / 显式关闭时静音。
"""
from __future__ import annotations

import os
import subprocess
import sys

SOUNDS = {
    "order": "/System/Library/Sounds/Tink.aiff",
    "open": "/System/Library/Sounds/Glass.aiff",
}


def alerts_enabled() -> bool:
    flag = os.environ.get("CRYPTO_ALERT_SOUND", "1").strip().lower()
    if flag in {"0", "false", "off", "no"}:
        return False
    return "unittest" not in sys.modules


def play_alert(kind: str) -> None:
    """非阻塞播放。kind 为 order（委托）或 open（开仓成交）。"""
    if not alerts_enabled():
        return
    path = SOUNDS.get(kind)
    if not path or not os.path.isfile(path):
        return
    try:
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return
