"""交易运行模式与安全闸门。

当前版本只允许 paper/testnet；主网 URL 即使被误填，也不会在默认配置下启用。
"""
from __future__ import annotations

import os
from enum import Enum
from urllib.parse import urlparse


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


def validate_exchange_target(base_url: str) -> None:
    """阻断默认配置下的 Binance 主网目标。"""
    mode = current_mode()
    if mode is TradingMode.LIVE:
        raise RuntimeError(
            "LIVE 模式未在本版本开放；请先完成 Testnet/模拟盘验收和人工安全审查"
        )
    if is_mainnet_url(base_url):
        raise RuntimeError(
            "检测到 Binance 主网 URL；当前框架仅允许 paper/testnet，已阻断连接"
        )


def startup_status() -> dict:
    mode = current_mode()
    return {
        "mode": mode.value,
        "live_allowed": False,
        "entries_default": False,
        "reason": "先完成 paper/testnet 验收；主网执行需要单独的人工审查与部署版本",
    }
