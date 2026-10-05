"""行情数据源解析 —— 保证「行情腿」与「下单腿」永远在同一个市场。

背景（2026-10-02 事故，务必读）：
    `shadow/live.py` 曾把 K 线地址**硬编码为主网** `https://fapi.binance.com`，
    而下单走测试网 `https://testnet.binancefuture.com`。
    结果是 bot 用主网的 K 线算 KDJ、却在测试网下单：

      * 信号比真实账户晚 2 根 K 线（30 分钟）
      * 那一笔空单：按测试网信号进场毛利 +103.5 点，
        按主网信号进场只有 +37.0 点，扣掉 84.8 点手续费后净亏
      * 出场那笔同样错位：主网 K 28.95 触发金叉，测试网同根 K 36.04 根本没触发

    根因不是策略，而是**行情地址和账户地址各有各的来源**。

本模块是行情地址的**唯一来源**，并且刻意做成**由账户地址反推**：
账户在哪，行情就在哪。两者不可能再分叉。

实测确认（2026-10-02）：
    * 主网 REST   https://fapi.binance.com            → 主网 WS wss://fstream.binance.com
    * 测试网 REST https://testnet.binancefuture.com   → 测试网 WS wss://fstream.binancefuture.com
      （`https://demo-fapi.binance.com` 与 legacy 测试网返回**完全相同**的 K 线，
        是同一个市场的两个域名；WS 侧 `wss://demo-fstream.binance.com` 同样一致。）
    * ⚠️ `wss://stream.binancefuture.com` 返回**另一个市场**的价格，禁止使用。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 地址表
# ---------------------------------------------------------------------------

MAINNET_REST = "https://fapi.binance.com"
MAINNET_WS = "wss://fstream.binance.com"

TESTNET_REST = "https://testnet.binancefuture.com"
TESTNET_WS = "wss://fstream.binancefuture.com"

# ---------------------------------------------------------------------------
# 外部情绪数据 —— 与交易市场**无关**，恒为主网
#
# `/futures/data/*`（多空账户比、持仓量历史）**只有主网提供**：
# 测试网对同一路径返回 301（跳官网页面），实测确认。
#
# 这类数据是外部市场情绪参考，不参与任何下单决策，也没有测试网对应物。
# 单独命名是为了避免与「交易市场」混淆 —— 它指向主网是**有意的**，
# 而不是 2026-10-02 那种「行情腿与下单腿错位」的事故。
# ---------------------------------------------------------------------------
SENTIMENT_REST = MAINNET_REST

# 测试网的备用域名（与 legacy 同市场，实测 K 线完全一致）。
# 仅用于「识别」, 不作为默认输出地址。
TESTNET_REST_ALIASES = ("testnet.binancefuture.com", "demo-fapi.binance.com")
TESTNET_WS_ALIASES = ("fstream.binancefuture.com", "demo-fstream.binance.com")

# 已知属于**其他市场**、绝不可当作测试网使用的地址。
# `stream.binancefuture.com` 实测报价与测试网 REST 不一致。
FORBIDDEN_HOSTS = ("stream.binancefuture.com",)

MARKET_MAINNET = "mainnet"
MARKET_TESTNET = "testnet"
MARKET_UNKNOWN = "unknown"


class MarketMismatchError(RuntimeError):
    """行情腿与下单腿不在同一个市场。宁可停机, 也不允许带着错位的数据下单。"""


def _host(url: str) -> str:
    return (urlparse((url or "").strip()).hostname or "").lower()


def market_of_host(host: str) -> str:
    """把主机名归类到市场。"""
    host = (host or "").lower()
    if host in TESTNET_REST_ALIASES or host in TESTNET_WS_ALIASES:
        return MARKET_TESTNET
    if host in {"fapi.binance.com", "fstream.binance.com", "api.binance.com"}:
        return MARKET_MAINNET
    return MARKET_UNKNOWN


def market_of_url(url: str) -> str:
    return market_of_host(_host(url))


def is_forbidden_host(url: str) -> bool:
    return _host(url) in FORBIDDEN_HOSTS


# ---------------------------------------------------------------------------
# 账户地址 → 行情地址
# ---------------------------------------------------------------------------


def rest_for_market(market: str) -> str:
    return MAINNET_REST if market == MARKET_MAINNET else TESTNET_REST


def ws_for_market(market: str) -> str:
    return MAINNET_WS if market == MARKET_MAINNET else TESTNET_WS


def sentiment_rest() -> str:
    """外部情绪数据基准地址。

    恒为主网 —— `/futures/data/*` 在测试网不存在（301）。
    这是**有意的固定值**, 不是市场错位: 该数据集只用于面板展示的
    情绪参考, 不参与下单决策。
    """
    return SENTIMENT_REST


def _configured_account_base_url() -> Optional[str]:
    """读取账户（下单）地址。按运行模式取对应市场的来源:

      * live → 主网: BINANCE_MAINNET_BASE_URL / secrets binance_mainnet_base_url,
        都没有时用主网默认地址（主网执行仍由 runtime_mode 双重确认把关）;
      * 其余 → 测试网: BINANCE_TESTNET_BASE_URL / secrets binance_testnet_base_url,
        都没有时用测试网默认地址。

    刻意不 import `config.secrets` / `config.review`, 以避免循环依赖,
    并保证在最小环境下也能解析。优先级与 `config.secrets` 一致:
    环境变量 > runtime/secrets.json。
    """
    mode = current_trading_mode()
    if mode == "live":
        env_name = "BINANCE_MAINNET_BASE_URL"
        secret_key = "binance_mainnet_base_url"
        fallback = MAINNET_REST
    else:
        env_name = "BINANCE_TESTNET_BASE_URL"
        secret_key = "binance_testnet_base_url"
        fallback = TESTNET_REST
    env = (os.environ.get(env_name) or "").strip()
    if env:
        return env

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    path = os.path.join(root, "runtime", "secrets.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        val = (data.get(secret_key) or "").strip()
        if val:
            return val
    except Exception:
        pass
    return fallback


def current_trading_mode() -> str:
    raw = (os.environ.get("TRADING_MODE") or "paper").strip().lower()
    return raw if raw in {"paper", "testnet", "live"} else "paper"


@dataclass(frozen=True)
class MarketEndpoints:
    """一个市场的一整套行情地址。"""

    market: str
    rest: str
    ws: str
    account_base_url: str
    source: str

    @property
    def is_testnet(self) -> bool:
        return self.market == MARKET_TESTNET

    @property
    def label(self) -> str:
        return {
            MARKET_MAINNET: "主网 (mainnet)",
            MARKET_TESTNET: "测试网 (testnet)",
            MARKET_UNKNOWN: "未知 (unknown)",
        }.get(self.market, self.market)

    def klines_url(self, interval: str = "15m", symbol: str = "BTCUSDT",
                   limit: int = 500) -> str:
        return (f"{self.rest}/fapi/v1/klines"
                f"?symbol={symbol}&interval={interval}&limit={int(limit)}")

    def describe(self) -> str:
        return (f"行情腿={self.market} rest={self.rest} ws={self.ws} | "
                f"下单腿={self.account_base_url} | 来源={self.source}")


def resolve_market_endpoints(
    account_base_url: Optional[str] = None,
) -> MarketEndpoints:
    """解析当前生效的行情地址。

    **账户地址是权威**: 行情必须跟着账户走。仅当账户地址缺失/无法识别时,
    才退回按 TRADING_MODE 推断, 并在 `source` 里标注退化原因。
    """
    base = (account_base_url or "").strip() or _configured_account_base_url()
    mode = current_trading_mode()

    if base:
        if is_forbidden_host(base):
            raise MarketMismatchError(
                f"账户地址 {base} 属于已知的异市场主机, 拒绝使用"
            )
        market = market_of_url(base)
        if market != MARKET_UNKNOWN:
            return MarketEndpoints(
                market=market,
                rest=rest_for_market(market),
                ws=ws_for_market(market),
                account_base_url=base,
                source="account_base_url",
            )

    # 退化路径：账户地址不可用，按模式推断
    market = MARKET_MAINNET if mode == "live" else MARKET_TESTNET
    return MarketEndpoints(
        market=market,
        rest=rest_for_market(market),
        ws=ws_for_market(market),
        account_base_url=base or "",
        source=("mode_fallback:账户地址缺失或不可识别, 按 TRADING_MODE 推断"),
    )


def resolve_for_account() -> MarketEndpoints:
    return resolve_market_endpoints()


# ---------------------------------------------------------------------------
# 一致性闸门
# ---------------------------------------------------------------------------


def assert_market_consistency(order_base_url: str,
                              market_url: str) -> None:
    """确认下单地址与行情地址属于同一个市场, 否则抛错。

    这是防止「2026-10-02 事故」复发的硬闸门 —— 在任何下单链路启动前调用。
    """
    if is_forbidden_host(market_url):
        raise MarketMismatchError(
            f"行情地址 {market_url} 属于已知的异市场主机"
        )
    order_market = market_of_url(order_base_url)
    data_market = market_of_url(market_url)
    if order_market == MARKET_UNKNOWN:
        raise MarketMismatchError(f"无法识别的下单地址: {order_base_url}")
    if data_market == MARKET_UNKNOWN:
        raise MarketMismatchError(f"无法识别的行情地址: {market_url}")
    if order_market != data_market:
        raise MarketMismatchError(
            f"行情腿与下单腿不在同一个市场: "
            f"下单={order_market} ({order_base_url}) "
            f"行情={data_market} ({market_url})。"
            f"已拒绝启动 —— 用主网 K 线在测试网下单会让信号错位。"
        )


def market_report() -> dict:
    """给启动横幅 / API 用的一行式市场说明。"""
    ep = resolve_for_account()
    return {
        "market": ep.market,
        "market_label": ep.label,
        "rest": ep.rest,
        "ws": ep.ws,
        "account_base_url": ep.account_base_url,
        "source": ep.source,
        "trading_mode": current_trading_mode(),
    }
