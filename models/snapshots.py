"""采集层原始快照与数据面评分结果的强类型模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class BlockTradeBias(Enum):
    """大额成交滚动窗口内的买卖偏向状态 (手册 5.3)."""
    NEUTRAL = "neutral"
    ACCUMULATION = "accumulation"       # 连续大买 + 价格不跌 → +70
    BUY_FOLLOW = "buy_follow"           # 大买 + 价格涨 → +50
    SELL_ABSORBED = "sell_absorbed"     # 连续大卖 + 价格不跌 → +40
    DISTRIBUTION = "distribution"       # 大卖 + 价格跌 → -60


class SessionZone(Enum):
    """交易时区 — 用于信号置信度调节."""
    US = "us"           # 美盘 ×1.0
    ASIA = "asia"       # 亚洲盘 ×0.7
    OTHER = "other"     # 其余时段按美盘处理


@dataclass
class BinanceMicroSnapshot:
    """Binance Futures 微观结构快照."""
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    funding_rate_period: Optional[float] = None      # 当期费率 (如 8h)
    funding_rate_annualized: Optional[float] = None  # 年化费率 (小数, 0.30 = 30%)
    funding_period_hours: Optional[float] = None
    next_funding_time_ms: Optional[int] = None

    bid_ask_ratio_2pct: Optional[float] = None       # ±2% Bid/Ask 量比
    bid_depth_2pct: Optional[float] = None
    ask_depth_2pct: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread: Optional[float] = None                   # best_ask - best_bid
    spread_vs_mean: Optional[float] = None           # spread / rolling_mean

    block_trade_bias: BlockTradeBias = BlockTradeBias.NEUTRAL
    block_buy_notional_usd: float = 0.0              # 窗口内大买名义额
    block_sell_notional_usd: float = 0.0
    block_trade_count: int = 0

    # CVD: 5min 滚动窗口内 taker 买名义 − taker 卖名义 (USD)
    cvd_5m_usd: Optional[float] = None

    orderbook_synced: bool = False
    event_time_ms: Optional[int] = None


@dataclass
class CoinGlassSnapshot:
    """CoinGlass 聚合衍生品快照. 缺字段保持 None → mapper 记 0."""
    open_interest_usd: Optional[float] = None
    oi_change_5m_pct: Optional[float] = None         # 小数, 0.03 = +3%
    oi_change_24h_pct: Optional[float] = None

    # 清算热力图: 相对现价 1~3% 带内的清算杠杆密度
    heatmap_above_intensity: Optional[float] = None  # 上方空头密集 [0, 1]
    heatmap_below_intensity: Optional[float] = None  # 下方多头密集 [0, 1]
    heatmap_magnet: Optional[float] = None           # [-1, +1]: +上方磁铁 / -下方磁铁

    liq_long_5m_usd: Optional[float] = None          # 5min 多头爆仓额
    liq_short_5m_usd: Optional[float] = None
    liq_total_5m_usd: Optional[float] = None
    # complete=窗口完整真零可成立; warmup/stale/error/partial/missing ≠ 真零
    liq_window_status: Optional[str] = None

    long_short_ratio: Optional[float] = None         # 全局账户多空比

    # 清算速度: 爆仓从峰值下降比例 [0, 1], None = 未知
    liquidation_speed_long_cleared: Optional[float] = None
    liquidation_speed_short_cleared: Optional[float] = None

    available: bool = False                          # 是否拿到过任何有效响应
    last_success_ts: Optional[int] = None
    last_error: Optional[str] = None


@dataclass
class DataSnapshot:
    """合并后的数据面输入快照."""
    binance: BinanceMicroSnapshot = field(default_factory=BinanceMicroSnapshot)
    coinglass: CoinGlassSnapshot = field(default_factory=CoinGlassSnapshot)
    deribit: Optional["DeribitSnapshot"] = None
    onchain: Optional["OnchainSnapshot"] = None
    session: SessionZone = SessionZone.US
    timestamp_ms: Optional[int] = None


@dataclass
class DataScoreResult:
    """数据面评分输出 — 只含 S_data, 不伪造完整 CS."""
    s_data: float                                    # [-100, +100]
    onchain_score: float = 0.0
    derivatives_score: float = 0.0
    microstructure_score: float = 0.0
    indicator_scores: Dict[str, float] = field(default_factory=dict)
    layer_scores: Dict[str, float] = field(default_factory=dict)
    missing_fields: List[str] = field(default_factory=list)
    spread_danger: bool = False                      # Spread > 5x
    black_swan_liq: bool = False                     # 5min 爆仓 > 2亿
    session_multiplier: float = 1.0
    confidence: float = 1.0                          # 可用权重占比 [0,1]
    reasoning: str = ""


@dataclass
class TechSnapshot:
    """技术面原始特征快照 (由 indicators.engine 填充)."""
    available: bool = False
    price: Optional[float] = None
    interval_bars: int = 0

    structure_bias: Optional[str] = None
    structure_score: float = 0.0
    ema21: Optional[float] = None
    ema50: Optional[float] = None
    ema200: Optional[float] = None
    ema_score: float = 0.0
    adx: Optional[float] = None
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    atr_mean: Optional[float] = None  # 近 20 期 ATR 均值 (高波动调节)
    boll_bandwidth: Optional[float] = None
    boll_squeeze: bool = False

    vwap: Optional[float] = None
    vwap_score: float = 0.0
    vp_poc: Optional[float] = None
    vp_hvn_low: Optional[float] = None
    vp_hvn_high: Optional[float] = None
    vp_score: float = 0.0
    nearest_support: Optional[float] = None
    nearest_resistance: Optional[float] = None
    sr_score: float = 0.0
    pdh: Optional[float] = None
    pdl: Optional[float] = None
    pdh_pdl_score: float = 0.0

    rsi: Optional[float] = None
    rsi_divergence_score: float = 0.0
    macd_hist: Optional[float] = None
    macd_score: float = 0.0
    volume_score: float = 0.0
    obv_score: float = 0.0

    pin_bar_score: float = 0.0
    engulfing_score: float = 0.0
    inside_bar_score: float = 0.0


@dataclass
class TechScoreResult:
    """技术面评分输出 — 只含 S_tech."""
    s_tech: float
    indicator_scores: Dict[str, float] = field(default_factory=dict)
    adx_multiplier: float = 1.0
    boll_multiplier: float = 1.0
    atr: Optional[float] = None          # 绝对 ATR (价格单位)
    atr_pct: Optional[float] = None      # ATR / price
    atr_mean: Optional[float] = None     # 近 20 期 ATR 均值
    missing_fields: List[str] = field(default_factory=list)
    confidence: float = 1.0
    reasoning: str = ""


# ---------------------------------------------------------------------------
# 预测面 / 消息面 / Deribit / Onchain
# ---------------------------------------------------------------------------

@dataclass
class PolymarketSnapshot:
    """Polymarket Gamma API 快照.

    注: CME FedWatch 无免费 API, fed_cut_prob 用 Polymarket Fed 市场代替.
    事件语义: market_id / event_type / expiry — 禁止跨 market_id 算增速.
    """
    btc_prob: Optional[float] = None              # "本月 > $X" 概率 [0,1]
    btc_prob_change_1h: Optional[float] = None    # 1h 变化 (百分点, 0.1 = +10pp)
    btc_market_slug: Optional[str] = None
    btc_market_question: Optional[str] = None
    btc_threshold_usd: Optional[float] = None
    market_id: Optional[str] = None               # condition_id
    event_type: Optional[str] = None              # above_at_expiry / touch_during / ...
    expiry_ms: Optional[int] = None
    remaining_hours: Optional[float] = None

    fed_cut_prob: Optional[float] = None          # 降息概率代理 [0,1]
    fed_hike_prob: Optional[float] = None         # 加息概率代理 [0,1]
    fed_market_slug: Optional[str] = None
    fed_market_question: Optional[str] = None

    available: bool = False
    last_success_ts: Optional[int] = None         # unix ms
    last_error: Optional[str] = None


@dataclass
class DeribitSnapshot:
    """Deribit 期权公开数据."""
    max_pain: Optional[float] = None              # USD
    mark_price: Optional[float] = None            # 用于算距离
    max_pain_distance: Optional[float] = None     # (price-max_pain)/max_pain
    iv: Optional[float] = None                    # DVOL / 100 → 小数
    expiry: Optional[str] = None
    available: bool = False
    last_success_ts: Optional[int] = None
    last_error: Optional[str] = None


@dataclass
class OnchainSnapshot:
    """免费链上/价格代理快照.

    诚实命名:
      * price_to_365d_avg — 原 mvrv_approx, 不是真实 MVRV
      * price_momentum_24h — 原 exchange_reserves_proxy, 不是交易所储备
    旧字段名保留为属性别名以兼容调用方.
    仍常驻 None: NUPL / LTH / SOPR / miner_reserves (无可靠免费源).
    """
    hashrate_ma30_change_pct: Optional[float] = None  # 小数, 0.05 = +5%
    whale_net_flow_btc: Optional[float] = None        # 正=流入交易所(看空)
    whale_transfer_direction: Optional[str] = None    # "to_exchange"|"from_exchange"|"none"
    whale_net_flow_usd: Optional[float] = None
    usdt_mint_24h: Optional[float] = None
    usdt_burn_24h: Optional[float] = None
    # 诚实代理名
    price_momentum_24h: Optional[float] = None   # [-1,1], +偏多动量
    price_to_365d_avg: Optional[float] = None    # 现价/365日均价
    # 兼容旧名
    exchange_reserves_proxy: Optional[float] = None
    mvrv_approx: Optional[float] = None
    available: bool = False
    last_success_ts: Optional[int] = None
    last_error: Optional[str] = None

    def sync_proxy_aliases(self) -> None:
        """双向同步新旧字段, 以诚实名为准."""
        if self.price_to_365d_avg is not None:
            self.mvrv_approx = self.price_to_365d_avg
        elif self.mvrv_approx is not None:
            self.price_to_365d_avg = self.mvrv_approx
        if self.price_momentum_24h is not None:
            self.exchange_reserves_proxy = self.price_momentum_24h
        elif self.exchange_reserves_proxy is not None:
            self.price_momentum_24h = self.exchange_reserves_proxy


@dataclass
class NewsSnapshot:
    """消息面原始输入 — 短/长期共用字段, mapper 按口径选用."""
    etf_daily_net_usd: Optional[float] = None
    etf_weekly_net_usd: Optional[float] = None
    etf_consecutive_inflow_weeks: int = 0
    dxy_change_5d: Optional[float] = None         # 小数
    dxy_source: Optional[str] = None              # "stooq"|"yahoo"
    months_since_halving: Optional[float] = None
    months_to_halving: Optional[float] = None
    regulation_score: Optional[float] = None      # 已映射到 [-100,100] 的低置信度分
    regulation_confidence: str = "none"           # "low"|"none"
    institutional_score: Optional[float] = None   # 来自 whale 方向 (net btc)
    usdt_net_mint_24h: Optional[float] = None
    # FRED 宏观代理
    cpi_yoy_change: Optional[float] = None        # 同比变化差, 下降=利好
    yield_curve_10y2y: Optional[float] = None     # 百分点
    fed_funds_rate: Optional[float] = None
    m2_yoy: Optional[float] = None                # M2 同比 (小数)
    pmi: Optional[float] = None                   # PMI / 就业代理指数
    # 短期突发 (CryptoPanic + RSS)
    breaking_sentiment: Optional[float] = None    # [-100,100] 加权情绪净值
    regulatory_event_score: Optional[float] = None
    macro_surprise_score: Optional[float] = None
    black_swan_score: Optional[float] = None
    event_severity: str = "none"                # none|normal|important|extreme
    recent_headlines: List[str] = field(default_factory=list)
    black_swan_event: bool = False
    available: bool = False
    last_success_ts: Optional[int] = None
    last_error: Optional[str] = None
    missing_fields: List[str] = field(default_factory=list)


@dataclass
class NewsScoreResult:
    s_news: float
    sub_scores: Dict[str, float] = field(default_factory=dict)
    missing_fields: List[str] = field(default_factory=list)
    confidence: float = 1.0
    reasoning: str = ""


@dataclass
class CryptoPanicSnapshot:
    """CryptoPanic 聚合突发快照."""
    breaking_sentiment: Optional[float] = None
    regulatory_event_score: Optional[float] = None
    macro_surprise_score: Optional[float] = None
    black_swan_score: Optional[float] = None
    event_severity: str = "none"
    recent_headlines: List[str] = field(default_factory=list)
    post_count: int = 0
    available: bool = False
    last_success_ts: Optional[int] = None
    last_error: Optional[str] = None


@dataclass
class PredictFunSnapshot:
    """Predict.fun / 币安预测市场 CRYPTO_UP_DOWN 快照.

    Up 概率 [0,1]: >0.5 看多, <0.5 看空.
    价格源多为 BINANCE BTCUSDT.
    """
    btc_up_prob_5m: Optional[float] = None
    btc_up_prob_15m: Optional[float] = None
    btc_up_prob_1h: Optional[float] = None
    btc_up_prob_1d: Optional[float] = None
    # 多窗口共识涨跌概率
    btc_up_prob: Optional[float] = None
    active_window: Optional[str] = None           # "5m"|"15m"|"1h"|"1d"
    market_question: Optional[str] = None
    market_id: Optional[int] = None
    start_price: Optional[float] = None
    price_feed_provider: Optional[str] = None     # 通常 BINANCE
    available: bool = False
    last_success_ts: Optional[int] = None
    last_error: Optional[str] = None
    source: str = "testnet"                       # "testnet"|"mainnet"


@dataclass
class FearGreedSnapshot:
    """Crypto Fear & Greed Index (alternative.me)."""
    value: Optional[float] = None                 # 0~100
    classification: Optional[str] = None          # Extreme Fear / Greed ...
    available: bool = False
    last_success_ts: Optional[int] = None
    last_error: Optional[str] = None


@dataclass
class MacroSnapshot:
    """FRED 宏观代理: CPI / 利差 / 联邦基金 / M2 / PMI."""
    cpi_yoy: Optional[float] = None               # 小数, 0.03 = 3%
    cpi_yoy_change: Optional[float] = None        # 近两期同比差 (下降=利好)
    yield_curve_10y2y: Optional[float] = None     # 百分点
    fed_funds_rate: Optional[float] = None        # 百分点
    m2_yoy: Optional[float] = None                # M2 同比 (小数)
    pmi: Optional[float] = None                   # INDPRO 同比 (ISM PMI 免费代理)
    available: bool = False
    last_success_ts: Optional[int] = None
    last_error: Optional[str] = None


@dataclass
class PredictionSnapshot:
    """预测面原始输入 (Predict.fun + Polymarket + Max Pain + F&G)."""
    polymarket: PolymarketSnapshot = field(default_factory=PolymarketSnapshot)
    predict_fun: PredictFunSnapshot = field(default_factory=PredictFunSnapshot)
    fear_greed: FearGreedSnapshot = field(default_factory=FearGreedSnapshot)
    max_pain_distance: Optional[float] = None
    mark_price: Optional[float] = None            # 用于 Polymarket 阈值距离校准
    available: bool = False
    last_success_ts: Optional[int] = None


@dataclass
class PredictionScoreResult:
    s_prediction: float
    sub_scores: Dict[str, float] = field(default_factory=dict)
    missing_fields: List[str] = field(default_factory=list)
    confidence: float = 1.0
    reasoning: str = ""
