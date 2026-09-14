"""V7 数据面映射锚点与采集常数 — 禁止在 mapper/collector 中硬编码魔法数字。"""

from typing import List, Tuple

# ---------------------------------------------------------------------------
# 采集常数
# ---------------------------------------------------------------------------

BLOCK_TRADE_NOTIONAL_USD = 500_000          # 大单门槛 (USD)
ORDERBOOK_BAND_PCT = 0.02                   # ±2% 盘口深度带宽
SPREAD_DANGER_MULT = 3.0                    # Spread > 3x 均值 → 方向分归零
SPREAD_BLACK_SWAN_MULT = 5.0                # Spread > 5x 均值 → 危险标记
OI_DROP_5M_PCT = 0.03                       # 5min OI 骤降阈值 3%
OI_EXTREME_BUILD_24H_PCT = 0.15             # 24h OI 极端堆积阈值 (约 15%)
LIQUIDATION_5M_USD = 50_000_000             # 5min 单边爆仓 > 5000万
BLACK_SWAN_LIQ_5M_USD = 200_000_000         # 5min 总爆仓 > 2亿 → 黑天鹅
HEATMAP_BAND_LOW_PCT = 0.01                 # 清算热力图近端 1%
HEATMAP_BAND_HIGH_PCT = 0.03                # 清算热力图远端 3%
FUNDING_PERIOD_HOURS_DEFAULT = 8.0          # Binance 默认资金费率结算周期
FUNDING_EXTREME_ANNUAL_PCT = 0.30           # 年化 |费率| > 30% 视为极端
FUNDING_NORMAL_ANNUAL_PCT = 0.10            # 年化 |费率| < 10% 视为正常
BLOCK_TRADE_WINDOW_SEC = 120                # 大单滚动窗口 (秒)
SPREAD_MEAN_WINDOW = 60                     # Spread 滚动均值样本数
SESSION_ASIA_MULTIPLIER = 0.7               # 亚洲盘信号折扣
SESSION_US_MULTIPLIER = 1.0                 # 美盘不打折

# CoinGlass 轮询
COINGLASS_BASE_URL = "https://open-api-v4.coinglass.com"
COINGLASS_POLL_INTERVAL_SEC = 30
COINGLASS_TIMEOUT_SEC = 15

# Binance
BINANCE_FUTURES_WS = "wss://fstream.binance.com/stream"
BINANCE_FUTURES_REST = "https://fapi.binance.com"
BINANCE_SYMBOL = "BTCUSDT"
BINANCE_DEPTH_LIMIT = 1000
BINANCE_RECONNECT_BASE_SEC = 1.0
BINANCE_RECONNECT_MAX_SEC = 60.0

# ---------------------------------------------------------------------------
# 锚点表: (raw_value, score) 线性插值, 两端钳制到 [-100, +100]
# ---------------------------------------------------------------------------

# 资金费率年化 (%): 手册 5.2
# 年化 < -30% → +90; -30%~-10% → +50; ±10% → 0; +10%~+30% → -50; > +30% → -90
FUNDING_RATE_ANCHORS: List[Tuple[float, float]] = [
    (-0.50, 90.0),
    (-0.30, 90.0),
    (-0.10, 50.0),
    (0.0, 0.0),
    (0.10, -50.0),
    (0.30, -90.0),
    (0.50, -90.0),
]

# Bid/Ask 深度比 (±2%): 手册 5.3
# > 2.0 → +60; 1.0~1.5 → +20; 0.8~1.0 → 0; 0.5~0.8 → -20; < 0.5 → -60
BID_ASK_RATIO_ANCHORS: List[Tuple[float, float]] = [
    (0.0, -60.0),
    (0.5, -60.0),
    (0.8, -20.0),
    (1.0, 0.0),
    (1.5, 20.0),
    (2.0, 60.0),
    (3.0, 60.0),
]

# 多空比 (散户反向指标): 手册 5.2
# < 0.5 → +60; 均衡 → 0; > 2.5 → -60
LONG_SHORT_RATIO_ANCHORS: List[Tuple[float, float]] = [
    (0.0, 60.0),
    (0.5, 60.0),
    (1.0, 0.0),
    (1.5, 0.0),
    (2.5, -60.0),
    (4.0, -60.0),
]

# 清算热力图磁铁强度 (上方空头密集 → 正分, 下方多头密集 → 负分)
# intensity: 归一化后 [-1, +1], +1 = 上方极度密集, -1 = 下方极度密集
HEATMAP_MAGNET_ANCHORS: List[Tuple[float, float]] = [
    (-1.0, -70.0),
    (-0.5, -35.0),
    (0.0, 0.0),
    (0.5, 35.0),
    (1.0, 70.0),
]

# ---------------------------------------------------------------------------
# 期权 / IV / Max Pain (手册 5.2)
# ---------------------------------------------------------------------------

# 价格相对 Max Pain 的偏离 ( (price - max_pain) / max_pain )
# 价格低于 Max Pain > 5% → +40; 接近 → 0; 高于 > 5% → -40
MAX_PAIN_DISTANCE_ANCHORS: List[Tuple[float, float]] = [
    (-0.15, 40.0),
    (-0.05, 40.0),
    (-0.01, 0.0),
    (0.01, 0.0),
    (0.05, -40.0),
    (0.15, -40.0),
]

# IV (小数, 0.40 = 40%): IV < 40% → +60; 40~60% → 0; 60~80% → -20; > 80% → -40
IV_ANCHORS: List[Tuple[float, float]] = [
    (0.0, 60.0),
    (0.40, 60.0),
    (0.50, 0.0),
    (0.60, 0.0),
    (0.70, -20.0),
    (0.80, -40.0),
    (1.20, -40.0),
]

# V8 粗略 MVRV 代理 (手册: <1 → +90; 1~1.5 → +60; 1.5~2.5 → 0; 2.5~3.5 → -40; >3.5 → -80)
MVRV_ANCHORS: List[Tuple[float, float]] = [
    (0.5, 90.0),
    (1.0, 90.0),
    (1.5, 60.0),
    (2.0, 0.0),
    (2.5, 0.0),
    (3.5, -40.0),
    (5.0, -80.0),
]

# 交易所存量代理: 偏离 [-1,+1], + = 流出交易所偏多
EXCHANGE_RESERVES_PROXY_ANCHORS: List[Tuple[float, float]] = [
    (-1.0, -70.0),
    (-0.5, -35.0),
    (0.0, 0.0),
    (0.5, 35.0),
    (1.0, 70.0),
]

# ---------------------------------------------------------------------------
# 预测面锚点 (手册第七节)
# ---------------------------------------------------------------------------

# Polymarket "本月 > $X" 概率 [0, 1] — 仅作无阈值/无现价时的兜底
PROBABILITY_ANCHORS: List[Tuple[float, float]] = [
    (0.0, -90.0),
    (0.15, -80.0),
    (0.40, -30.0),
    (0.50, 0.0),
    (0.60, 30.0),
    (0.85, 80.0),
    (1.0, 100.0),
]

# Polymarket 阈值距离 → 公平 Yes 概率 (约 2 日窗口, BTC 日波 ~2.5%)
# |rel| = |threshold - mark| / mark → 上行阈值的「自然」中性概率
NEUTRAL_PROB_BY_REL: List[Tuple[float, float]] = [
    (0.00, 0.50),
    (0.01, 0.35),
    (0.02, 0.25),
    (0.03, 0.18),
    (0.05, 0.10),
    (0.10, 0.03),
    (0.20, 0.01),
]

# (实际概率 - 公平概率) → 方向分; 相对偏差为 0 时中性
RELATIVE_PROB_DELTA_ANCHORS: List[Tuple[float, float]] = [
    (-0.40, -80.0),
    (-0.20, -50.0),
    (-0.10, -25.0),
    (-0.05, -10.0),
    (0.0, 0.0),
    (0.05, 10.0),
    (0.10, 25.0),
    (0.20, 50.0),
    (0.40, 80.0),
]

# Predict.fun CRYPTO_UP_DOWN Up 概率 [0, 1]
PREDICT_FUN_UP_ANCHORS: List[Tuple[float, float]] = [
    (0.0, -90.0),
    (0.30, -60.0),
    (0.45, -20.0),
    (0.50, 0.0),
    (0.55, 20.0),
    (0.70, 60.0),
    (1.0, 90.0),
]

# Fear & Greed Index [0, 100]: 恐惧偏多(逆向), 贪婪偏空
# 极端恐惧 → 买入机会; 极端贪婪 → 见顶风险
FEAR_GREED_ANCHORS: List[Tuple[float, float]] = [
    (0.0, 80.0),
    (20.0, 50.0),
    (40.0, 15.0),
    (50.0, 0.0),
    (60.0, -15.0),
    (80.0, -50.0),
    (100.0, -80.0),
]

# CVD 5min 名义额变化 (USD): 买压正 → +, 卖压负 → -
CVD_5M_ANCHORS: List[Tuple[float, float]] = [
    (-50_000_000, -80.0),
    (-10_000_000, -40.0),
    (-2_000_000, 0.0),
    (2_000_000, 0.0),
    (10_000_000, 40.0),
    (50_000_000, 80.0),
]

# V8 连续化: OI 5m 变化 (%) — 骤降偏多(出清), 堆积看费率方向由 mapper 处理符号
OI_CHANGE_5M_ANCHORS: List[Tuple[float, float]] = [
    (-0.08, 80.0),   # 暴跌 8% → 强出清
    (-0.03, 60.0),   # 骤降 3%
    (-0.01, 20.0),
    (0.0, 0.0),
    (0.02, -10.0),
    (0.05, -30.0),
    (0.15, -50.0),   # 极端堆积
]

# 5min 单边爆仓 USD → 连续分 (多头出清为正)
LIQ_REALTIME_USD_ANCHORS: List[Tuple[float, float]] = [
    (0.0, 0.0),
    (10_000_000, 20.0),
    (50_000_000, 70.0),
    (150_000_000, 90.0),
]

# 清算速度: 从峰值清除比例 [0,1]
LIQ_SPEED_CLEARED_ANCHORS: List[Tuple[float, float]] = [
    (0.0, 0.0),
    (0.40, 30.0),
    (0.80, 80.0),
    (1.0, 95.0),
]

# 巨鲸净流入交易所 BTC (正=流入交易所=看空)
WHALE_NET_BTC_ANCHORS: List[Tuple[float, float]] = [
    (-5000.0, 90.0),   # 大幅提出
    (-1000.0, 70.0),
    (-200.0, 20.0),
    (0.0, 0.0),
    (200.0, -20.0),
    (1000.0, -70.0),
    (5000.0, -90.0),
]

# 大单 bias 枚举值 → 连续分 (仍用字典, 但 mapper 可按名义叠加)
BLOCK_TRADE_SCORE_ANCHORS = {
    "neutral": 0.0,
    "accumulation": 70.0,
    "buy_follow": 50.0,
    "sell_absorbed": 40.0,
    "distribution": -60.0,
}

# CPI 同比变化差 (小数): 通胀回落 → +; 升温 → -
# 例: 上期 3.2% → 本期 2.8% → change = -0.004 → 利好
CPI_YOY_CHANGE_ANCHORS: List[Tuple[float, float]] = [
    (-0.02, 70.0),
    (-0.005, 40.0),
    (0.0, 0.0),
    (0.005, -40.0),
    (0.02, -70.0),
]

# 10Y-2Y 利差 (百分点): 正且走阔偏多; 倒挂偏空
YIELD_CURVE_ANCHORS: List[Tuple[float, float]] = [
    (-1.5, -60.0),
    (-0.5, -30.0),
    (0.0, 0.0),
    (0.5, 20.0),
    (1.5, 40.0),
]

# 1h 概率变化 (百分点, 0.30 = +30pp)
PROBABILITY_CHANGE_ANCHORS: List[Tuple[float, float]] = [
    (-0.50, -100.0),
    (-0.30, -90.0),
    (-0.10, -30.0),
    (0.0, 0.0),
    (0.10, 30.0),
    (0.30, 90.0),
    (0.50, 100.0),
]

# FedWatch 代理: 同时有 cut 与 hike 时用 (cut - hike) ∈ [-1, +1]
FEDWATCH_PROXY_ANCHORS: List[Tuple[float, float]] = [
    (-1.0, -90.0),
    (-0.85, -80.0),
    (-0.60, -50.0),
    (0.0, 0.0),
    (0.60, 50.0),
    (0.85, 80.0),
    (1.0, 90.0),
]

# 仅有「降息」合约时: 低降息概率 ≠ 加息 (手册中间档是「不确定」→ 0)
FEDWATCH_CUT_ONLY_ANCHORS: List[Tuple[float, float]] = [
    (0.0, 0.0),
    (0.40, 0.0),
    (0.60, 40.0),
    (0.85, 80.0),
    (1.0, 90.0),
]

# 仅有「加息」合约
FEDWATCH_HIKE_ONLY_ANCHORS: List[Tuple[float, float]] = [
    (0.0, 0.0),
    (0.40, 0.0),
    (0.60, -40.0),
    (0.85, -80.0),
    (1.0, -90.0),
]

# ---------------------------------------------------------------------------
# 消息面锚点 (手册第四节)
# ---------------------------------------------------------------------------

# ETF 单日净流入 (USD): >2亿 → +70; ±5000万 → 0; <-2亿 → -70
# 周净流入另有规则, mapper 内组合
ETF_DAILY_FLOW_ANCHORS: List[Tuple[float, float]] = [
    (-1_000_000_000, -90.0),
    (-200_000_000, -70.0),
    (-50_000_000, 0.0),
    (50_000_000, 0.0),
    (200_000_000, 70.0),
    (1_000_000_000, 90.0),
]

# DXY 5日变化率 (小数): 走弱 → 多; 走强 → 空 (货币政策代理)
DXY_TREND_ANCHORS: List[Tuple[float, float]] = [
    (-0.03, 70.0),
    (-0.01, 30.0),
    (0.0, 0.0),
    (0.01, -30.0),
    (0.03, -70.0),
]

# 算力 30日均线变化率: >5% → +30; 平稳 → 0; <-10% → -30
HASHRATE_CHANGE_ANCHORS: List[Tuple[float, float]] = [
    (-0.20, -30.0),
    (-0.10, -30.0),
    (0.0, 0.0),
    (0.05, 30.0),
    (0.15, 30.0),
]

# USDT 24h 净铸造 (USD): >2亿铸造 → +50; >5亿 → +80; 销毁对称负
USDT_NET_MINT_ANCHORS: List[Tuple[float, float]] = [
    (-500_000_000, -80.0),
    (-200_000_000, -50.0),
    (0.0, 0.0),
    (200_000_000, 50.0),
    (500_000_000, 80.0),
]

# M2 同比 (小数): >5% → +60; 0~5% → +30; -2~0% → 0; <-2% → -40
M2_YOY_ANCHORS: List[Tuple[float, float]] = [
    (-0.05, -40.0),
    (-0.02, -40.0),
    (0.0, 0.0),
    (0.05, 30.0),
    (0.08, 60.0),
    (0.15, 60.0),
]

# PMI 代理: FRED 已下架 ISM NAPM; 用工业产出同比 INDPRO 作免费代理
# 映射在 mapper 用 INDPRO_YOY_ANCHORS (字段仍名 pmi 以兼容面板)
INDPRO_YOY_ANCHORS: List[Tuple[float, float]] = [
    (-0.05, -30.0),
    (-0.02, -30.0),
    (0.0, 0.0),
    (0.02, 15.0),
    (0.04, 30.0),
    (0.08, 30.0),
]
# 保留旧名别名, 避免外部 import 断裂
PMI_ANCHORS = INDPRO_YOY_ANCHORS

# ---------------------------------------------------------------------------
# 消息面可靠性 / 突发关键词
# ---------------------------------------------------------------------------

# 域名可信度 (子串匹配, 取最高命中)
SOURCE_CREDIBILITY: dict = {
    # Tier 1
    "sec.gov": 1.0,
    "federalreserve.gov": 1.0,
    "cftc.gov": 1.0,
    "xinhuanet.com": 1.0,
    "news.cn": 1.0,
    "reuters.com": 1.0,
    "bloomberg.com": 1.0,
    "coindesk.com": 1.0,
    # Tier 2
    "cointelegraph.com": 0.7,
    "theblock.co": 0.7,
    "decrypt.co": 0.7,
    "bitcoinmagazine.com": 0.7,
    "cryptobriefing.com": 0.6,
    "bitcoinmagazine": 0.7,
    "ambcrypto.com": 0.6,
    "coingape.com": 0.6,
}
SOURCE_CREDIBILITY_DEFAULT = 0.4
MULTI_SOURCE_BOOST = 1.3
SINGLE_LOW_SOURCE_DAMPEN = 0.5
LOW_SOURCE_THRESHOLD = 0.45

# 严重度关键词
BREAKING_EXTREME_KEYWORDS = (
    "ban", "hack", "exploit", "arrest", "sec charges", "sec charge",
    "war", "invasion", "collapse", "insolvency", "bankruptcy",
    "emergency", "black swan", "depeg",
)
BREAKING_IMPORTANT_KEYWORDS = (
    "regulation", "etf", "approval", "sanction", "lawsuit",
    "subpoena", "fed rate", "rate cut", "rate hike", "fomc",
    "cpi", "nonfarm", "payroll", "inflation",
)
BREAKING_BULL_KEYWORDS = (
    "etf approval", "spot etf", "approval", "inflow", "adopt",
    "reserve", "clarity", "dismiss", "win", "partnership",
    "launch", "integration",
)
BREAKING_BEAR_KEYWORDS = (
    "hack", "exploit", "ban", "sue", "charge", "crackdown",
    "delist", "outflow", "sell-off", "selloff", "liquidation",
    "fraud", "probe", "investigation", "sanction",
)
REGULATORY_KEYWORDS = (
    "sec", "cftc", "regulation", "regulatory", "lawsuit",
    "enforcement", "ban", "approval", "etf", "legislation",
    "congress", "bill", "sanction",
)
MACRO_SURPRISE_KEYWORDS = (
    "cpi", "inflation", "nonfarm", "payroll", "gdp", "pmi",
    "fomc", "rate cut", "rate hike", "fed", "jobs report",
    "unemployment", "pce",
)

# 时效衰减: (max_age_hours, multiplier)
NEWS_AGE_DECAY: List[Tuple[float, float]] = [
    (1.0, 1.0),
    (4.0, 0.5),
    (12.0, 0.2),
    (999.0, 0.0),
]

# ---------------------------------------------------------------------------
# 采集端点与轮询
# ---------------------------------------------------------------------------

POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
POLYMARKET_POLL_SEC = 30.0  # 短期 1h: 配合预测面提权
POLYMARKET_PROB_HISTORY_SEC = 3600.0

# Predict.fun (币安预测市场底层) — 默认 testnet, 有 key 后切 mainnet
PREDICT_FUN_TESTNET_URL = "https://api-testnet.predict.fun"
PREDICT_FUN_MAINNET_URL = "https://api.predict.fun"
PREDICT_FUN_POLL_SEC = 20.0  # 15m 盘 + 1h 决策窗口: 20s 刷新

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
FEAR_GREED_POLL_SEC = 3600.0

DEFI_LLAMA_TETHER_URL = "https://stablecoins.llama.fi/stablecoin/1"
DEFI_LLAMA_POLL_SEC = 300.0

# FRED CSV 免费下载 (无需 API key)
FRED_CSV_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_POLL_SEC = 3600.0

DERIBIT_REST = "https://www.deribit.com/api/v2"
DERIBIT_POLL_SEC = 60.0

FARSIDE_BTC_URL = "https://farside.co.uk/btc/"
STOOQ_DXY_URL = "https://stooq.com/q/d/l/?s=dx.f&i=d"
YAHOO_DXY_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"
    "?interval=1d&range=1mo"
)
NEWS_POLL_SEC = 30.0  # 短期 1h: 突发 30s
NEWS_POLL_LONG_SEC = 300.0  # 长期宏观消息面

CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
CRYPTOPANIC_POLL_SEC = 30.0

MEMPOOL_HASHRATE_URL = "https://mempool.space/api/v1/mining/hashrate/1m"
WHALE_ALERT_FREE_URL = "https://api.whale-alert.io/v1/transactions"
ONCHAIN_POLL_SEC = 300.0  # 长期链上日级变化; 短期也够用 (巨鲸异动靠 WS/REST 按需)
WHALE_MIN_USD = 1_000_000          # 巨鲸门槛 (USD 名义)
WHALE_EXCHANGE_BTC_THRESHOLD = 1000.0  # >1k BTC 进出交易所才记方向分

# BTC 减半日历 (UTC 日期 YYYY-MM-DD)
# 工程近似: 下次预计减半约 2028-04; 上次 2024-04-20
BTC_LAST_HALVING_DATE = "2024-04-20"
BTC_NEXT_HALVING_DATE = "2028-04-17"

# RSS — 政府源 + 主流媒体 (补 CryptoPanic 盲区 / 交叉验证)
# 已剔除失效 URL (CFTC 旧路径 / 新华社英文 RSS 404)
REGULATION_RSS_URLS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.theblock.co/rss.xml",
    "https://decrypt.co/feed",
    "https://www.sec.gov/news/pressreleases.rss",
    "https://www.federalreserve.gov/feeds/press_all.xml",
]
REGULATION_BULL_KEYWORDS = (
    "etf approval", "spot etf", "legislation pass", "clarity act",
    "lawsuit dismiss", "sec settle", "regulatory clarity",
)
REGULATION_BEAR_KEYWORDS = (
    "sec sue", "sec charge", "ban crypto", "exchange lawsuit",
    "enforcement action", "subpoena", "crackdown", "delist",
)
