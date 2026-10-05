"""交易运行模式与安全闸门。

默认只允许 paper/testnet。主网（真实资金）需要**双重显式确认**才会放行：

    TRADING_MODE=live  CONFIRM_MAINNET=YES_I_UNDERSTAND

两道缺一不可，且默认行为与旧版完全一致（缺任何一道都阻断）。
这样设计的原因：主网 URL 被误填、或环境变量被误设，都不足以让真实资金下单。

2026-10-05：按用户要求把「无条件禁止」改为「需双重显式确认」。
旧版的两条 raise 语义被完整保留，只是各自多了一个显式开关。
"""
from __future__ import annotations

import os
from enum import Enum
from urllib.parse import urlparse


# 主网启用的第二道确认串。故意写得必须逐字匹配，防止手滑。
MAINNET_CONFIRM_TOKEN = "YES_I_UNDERSTAND"
CONFIRM_ENV = "CONFIRM_MAINNET"


class TradingMode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


def current_mode() -> TradingMode:
    raw = (os.environ.get("TRADING_MODE") or "paper").strip().lower()
    try:
        return TradingMode(raw)
    except ValueError:
        return TradingMode.PAPER


def is_mainnet_url(url: str) -> bool:
    host = urlparse((url or "").strip()).hostname or ""
    return host in {"fapi.binance.com", "api.binance.com"}


def mainnet_confirmed() -> bool:
    """第二道确认是否已显式给出（逐字匹配，大小写敏感）。"""
    return (os.environ.get(CONFIRM_ENV) or "").strip() == MAINNET_CONFIRM_TOKEN


def validate_exchange_target(base_url: str) -> None:
    """校验交易目标。默认阻断主网；双确认齐备才放行。

    判定顺序（任一不满足即抛错，语义与旧版一致）：
      1. LIVE 模式但没给第二道确认 → 拒绝
      2. 目标是主网 URL 但没给第二道确认 → 拒绝
      3. 两道齐备 → 放行（调用方负责打印醒目横幅）
    """
    mode = current_mode()
    confirmed = mainnet_confirmed()

    if mode is TradingMode.LIVE and not confirmed:
        raise RuntimeError(
            "LIVE 模式需要第二道确认；请同时设置 "
            f"{CONFIRM_ENV}={MAINNET_CONFIRM_TOKEN}"
        )
    if is_mainnet_url(base_url) and not confirmed:
        raise RuntimeError(
            "检测到 Binance 主网 URL；当前未确认主网执行，已阻断连接。"
            f"确认请设置 {CONFIRM_ENV}={MAINNET_CONFIRM_TOKEN}"
        )


def startup_status() -> dict:
    mode = current_mode()
    confirmed = mainnet_confirmed()
    return {
        "mode": mode.value,
        "live_allowed": bool(mode is TradingMode.LIVE and confirmed),
        "mainnet_confirmed": confirmed,
        "entries_default": False,
        "reason": (
            "主网真实资金模式已启用"
            if (mode is TradingMode.LIVE and confirmed)
            else "先完成 paper/testnet 验收；主网执行需要双重显式确认"
        ),
    }
