"""复盘 / 自我改进回路配置 — 所有阈值集中于此, 禁止在业务代码中硬编码魔法数字。

回路 (self-recursive improvement loop):
    每笔成交
      → 记录四面 S_i、CS、决策、ATR、入场价
      → 按「事后价格」结算对错 (先触目标=对 / 先触止损=错 / 都没触=无效)
      → 错误样本写入复盘池
      → 有效样本 (对+错) 累积到 VALID_SAMPLE_TARGET
      → DeepSeek 输出参数微调建议 (只出建议)
      → 人工在面板点「采纳」后才写入新版配置, 旧版自动留档可回滚

设计要点:
  * 分母是「有效样本」而不是成交笔数 — 横盘与被覆盖的笔不计入胜率。
  * 结算口径本身不参与调参 (否则是自证循环)。
  * 模型只能在 TUNABLE_PARAMS 白名单内提议, 且受 PROPOSAL_GUARD 夹取。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 一、结算口径 (按事后价格)
# ---------------------------------------------------------------------------
# 一笔交易在窗口内先摸到哪一边, 就判哪一边。对称倍数保证「对/错」不预设偏向。
SETTLE_CONFIG: Dict[str, Dict[str, Any]] = {
    "short_term": {
        "window_hours": 1.0,        # 成交后观察窗口 = 持仓决策窗口 (1 小时)
        "kline_interval": "1h",     # V8.2: 与 1h 持仓窗口对齐 (原 5m ATR 过小)
        "target_atr_mult": 1.0,     # 先摸到 entry ± target×ATR → 对
        "stop_atr_mult": 1.0,       # 先摸到 entry ∓ stop×ATR   → 错
        "atr_period": 14,
    },
    "long_term": {
        "window_hours": 720.0,      # 30 天 (月级持仓口径)
        "kline_interval": "4h",
        "target_atr_mult": 2.0,
        "stop_atr_mult": 2.0,
        "atr_period": 14,
    },
}
ATR_FALLBACK_PCT = 0.01             # 拿不到 ATR 时用 1% 名义波动兜底
AMBIGUOUS_COUNTS_AS = "wrong"       # 同一根 K 线内同时触及目标与止损 → 保守记错

# 只有这四档才算「真的成交」。观望 (WATCH_*) 与中性 (NEUTRAL) 没开仓,
# 事后价格再准也不该拿来回测对错 —— 否则会往分母里灌一堆「从未发生的交易」。
ACTIONABLE_DECISIONS = frozenset({
    "STRONG_LONG", "STANDARD_LONG", "STRONG_SHORT", "STANDARD_SHORT",
})

# ---------------------------------------------------------------------------
# 二、复盘触发条件
# ---------------------------------------------------------------------------
VALID_SAMPLE_TARGET = 100           # 有效样本达标线 (对 + 错), 不含无效/排除
MIN_NEW_ERRORS_FOR_RERUN = 10       # 上版建议后又攒够多少错单才允许再跑
MAX_ERRORS_IN_PAYLOAD = 40          # 送进模型的错单上限 (取最近 N 笔)
MAX_TRADES_IN_PAYLOAD = 120         # 送进模型的总样本上限, 控 token

# ---------------------------------------------------------------------------
# 三、DeepSeek
# ---------------------------------------------------------------------------
# API key 不入库、不落盘: 由面板输入 → 本机后端仅驻内存 → 转发 DeepSeek。
# 也支持 DEEPSEEK_API_KEY 环境变量回退 (优先级低于面板输入)。
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-chat"
DEEPSEEK_MODEL_CHOICES = ["deepseek-chat", "deepseek-reasoner"]
DEEPSEEK_TEMPERATURE = 0.2          # 评审任务要稳定, 不要发挥
DEEPSEEK_MAX_TOKENS = 4000
DEEPSEEK_TIMEOUT_SEC = 120
DEEPSEEK_VERIFY_MAX_TOKENS = 8

# ---------------------------------------------------------------------------
# 四、建议护栏 — 模型输出必须过这几关才会展示在面板上
# ---------------------------------------------------------------------------
PROPOSAL_GUARD: Dict[str, Any] = {
    "max_params_per_set": 5,        # 一次最多改 5 个参数, 防止大爆炸式重写
    "max_relative_change": 0.25,    # 单参数单次最多动 ±25%
    "weights_sum_tolerance": 1e-6,  # 维度权重之和必须仍为 1.0
    "forbid_threshold_cross": True, # 阈值不得越过相邻档位 (保证档位不空)
}

# ---------------------------------------------------------------------------
# 五、可调参数白名单 — 模型只能动这些, 且必须在 [min, max] 内
# ---------------------------------------------------------------------------
# key 为点分路径, 与 config/weights.py 的常量名一一对应, 便于机械写回。
TUNABLE_PARAMS: Dict[str, Dict[str, Any]] = {
    # --- 维度权重 (短期) ---
    "DIMENSION_WEIGHTS.short_term.news":       {"min": 0.05, "max": 0.40, "group": "dim_weight", "horizon": "short_term", "dim": "news"},
    "DIMENSION_WEIGHTS.short_term.data":       {"min": 0.20, "max": 0.60, "group": "dim_weight", "horizon": "short_term", "dim": "data"},
    "DIMENSION_WEIGHTS.short_term.tech":       {"min": 0.05, "max": 0.40, "group": "dim_weight", "horizon": "short_term", "dim": "tech"},
    "DIMENSION_WEIGHTS.short_term.prediction": {"min": 0.05, "max": 0.35, "group": "dim_weight", "horizon": "short_term", "dim": "prediction"},
    # --- 维度权重 (长期) ---
    "DIMENSION_WEIGHTS.long_term.news":        {"min": 0.15, "max": 0.50, "group": "dim_weight", "horizon": "long_term", "dim": "news"},
    "DIMENSION_WEIGHTS.long_term.data":        {"min": 0.15, "max": 0.45, "group": "dim_weight", "horizon": "long_term", "dim": "data"},
    "DIMENSION_WEIGHTS.long_term.tech":        {"min": 0.05, "max": 0.35, "group": "dim_weight", "horizon": "long_term", "dim": "tech"},
    "DIMENSION_WEIGHTS.long_term.prediction":  {"min": 0.05, "max": 0.35, "group": "dim_weight", "horizon": "long_term", "dim": "prediction"},
    # --- 决策阈值 ---
    "DECISION_THRESHOLDS.strong_long":    {"min": 45, "max": 80, "group": "threshold"},
    "DECISION_THRESHOLDS.standard_long":  {"min": 20, "max": 55, "group": "threshold"},
    "DECISION_THRESHOLDS.watch_long":     {"min": 5,  "max": 25, "group": "threshold"},
    "DECISION_THRESHOLDS.standard_short": {"min": -55, "max": -20, "group": "threshold"},
    "DECISION_THRESHOLDS.strong_short":   {"min": -80, "max": -45, "group": "threshold"},
    # --- 安全阀 ---
    "SAFETY_VALVE_THRESHOLD": {"min": 30, "max": 70, "group": "safety_valve"},
    # --- 技术面调节因子 ---
    "ADX_BOOST":          {"min": 1.0, "max": 1.6, "group": "tech_mult"},
    "ADX_DAMPEN":         {"min": 0.3, "max": 0.9, "group": "tech_mult"},
    "BOLL_SQUEEZE_BOOST": {"min": 1.0, "max": 1.5, "group": "tech_mult"},
}

# 权重类参数需要整组校验 (和必须为 1.0), 单独列出便于校验器分组
WEIGHT_GROUPS = {
    "short_term": [
        "DIMENSION_WEIGHTS.short_term.news",
        "DIMENSION_WEIGHTS.short_term.data",
        "DIMENSION_WEIGHTS.short_term.tech",
        "DIMENSION_WEIGHTS.short_term.prediction",
    ],
    "long_term": [
        "DIMENSION_WEIGHTS.long_term.news",
        "DIMENSION_WEIGHTS.long_term.data",
        "DIMENSION_WEIGHTS.long_term.tech",
        "DIMENSION_WEIGHTS.long_term.prediction",
    ],
}

# 阈值必须保持的单调顺序 (由低到高), 用于 forbid_threshold_cross 校验
THRESHOLD_ORDER = [
    "DECISION_THRESHOLDS.strong_short",
    "DECISION_THRESHOLDS.standard_short",
    "DECISION_THRESHOLDS.watch_long",
    "DECISION_THRESHOLDS.standard_long",
    "DECISION_THRESHOLDS.strong_long",
]

# ---------------------------------------------------------------------------
# 六、存储路径与面板后端
# ---------------------------------------------------------------------------
JOURNAL_PATH = PROJECT_ROOT / "runtime" / "review" / "journal.jsonl"
PROPOSALS_DIR = PROJECT_ROOT / "runtime" / "review" / "proposals"
VERSIONS_DIR = PROJECT_ROOT / "config" / "weights_versions"
ACTIVE_VERSION_FILE = VERSIONS_DIR / "ACTIVE.json"

PANEL_HOST = "127.0.0.1"            # 只监听本机, 不对外网暴露
PANEL_PORT = 8787
PANEL_HTML = PROJECT_ROOT / "btc-four-face-monitor.html"

# ---------------------------------------------------------------------------
# 七、自我递归改进 · 收敛控制 (元评审)
# ---------------------------------------------------------------------------
INITIAL_MAX_RELATIVE_CHANGE = 0.25   # 首版最多改 ±25%
DECAY_FACTOR = 0.80                  # 每采纳一版, 学习率 ×0.8
MIN_MAX_RELATIVE_CHANGE = 0.05       # 地板 5% — 永远保留微调能力
OSCILLATION_LOOKBACK = 4             # 看最近 N 个版本的改动方向
OSCILLATION_SIGN_CHANGES = 2         # 方向反转 >= 此值 → 锁定参数
OSCILLATION_LOCK_VERSIONS = 2        # 锁定持续几个版本
PERF_GATE_TOLERANCE = 0.02           # 性能门: 允许 2% 统计噪声
ROLLBACK_SAMPLE_SIZE = 50            # 新版本跑满 N 笔有效样本后评估
ROLLBACK_WINRATE_DROP = 0.05         # 胜率比父版低 5% → 自动回滚
MAX_CONSECUTIVE_ROLLBACKS = 3        # 连续回滚此次数 → 进入观察模式
AUTO_ACCEPT_PROPOSALS = False        # 建议模式: AI 调参不自动写入交易配置
DAILY_SUMMARY_HOUR_CST = 2           # 每日总结触发小时 (CST/UTC+8)
DAILY_SUMMARIES_DIR = PROJECT_ROOT / "runtime" / "review" / "daily_summaries"
META_STATE_PATH = PROJECT_ROOT / "runtime" / "review" / "meta_state.json"

# ---------------------------------------------------------------------------
# 八、模拟盘交易
# ---------------------------------------------------------------------------
BINANCE_TESTNET_DEFAULT_BASE = "https://testnet.binancefuture.com"
TRADING_SYMBOL = "BTCUSDT"
LEVERAGE_SHORT_TERM = 2
LEVERAGE_LONG_TERM = 3  # V8: 从 5x 降到 3x, 敞口上限由风险预算约束
# 名义仓位占可用余额比例 — 仅作上限兜底; 真正定仓看 RISK_PER_TRADE
POSITION_NOTIONAL_PCT = {
    "short_term": 0.10,
    "long_term": 0.15,
}
MIN_NOTIONAL_USDT = 100.0
TRADING_HISTORY_PATH = PROJECT_ROOT / "runtime" / "review" / "trading_history.jsonl"
TRADE_LEDGER_PATH = PROJECT_ROOT / "runtime" / "review" / "trade_ledger.jsonl"
SIGNAL_RESEARCH_PATH = PROJECT_ROOT / "runtime" / "review" / "signal_research.jsonl"
POSITIONS_PATH = PROJECT_ROOT / "runtime" / "review" / "positions.json"

# 独立风控
RISK_GUARDIAN_INTERVAL_SEC = 5.0

# 各源 staleness 上限 (秒) — 超限阻断新开仓
STALENESS_LIMITS: Dict[str, float] = {
    "mark_price": 60.0,
    "binance": 60.0,
    "klines": 300.0,
    "funding": 600.0,
    "polymarket": 600.0,
    "predict_fun": 300.0,
    "news": 900.0,
    "onchain": 3600.0,
    "default": 300.0,
}

# ---------------------------------------------------------------------------
# 九、V8.1 核心交易参数: 单笔风险预算 → 仓位
# 仓位 qty = (权益 × risk_pct) / (ATR × stop_atr_mult)
# 止盈止损价位 = entry ± ATR × 倍数  (与结算口径对齐)
# ---------------------------------------------------------------------------
RISK_PER_TRADE_PCT = {
    "short_term": 0.005,   # 单笔最多亏账户权益的 0.5%
    "long_term": 0.010,    # 单笔最多亏 1.0%
}
# 名义敞口硬顶 (防止 ATR 极小时仓位爆炸)
MAX_NOTIONAL_PCT = {
    "short_term": 0.50,   # V8.2: 0.20→0.50, 避免 1h ATR 下风险预算被架空
    "long_term": 0.40,    # V8.2: 0.30→0.40
}
# 双 horizon 合计名义敞口上限 (相对权益)
TOTAL_MAX_NOTIONAL_PCT = 0.60

EXIT_STRATEGY: Dict[str, Dict[str, Any]] = {
    "short_term": {
        # ATR 倍数优先; pct 作无 ATR 时回退
        "tp1_atr": 1.2,             # V8.2: 0.8→1.2, RR=1.2:1
        "tp1_pct": 0.012,
        "tp1_close_pct": 0.50,
        "tp2_atr": 2.5,             # V8.2: 1.5→2.5
        "tp2_pct": 0.025,
        "tp2_close_pct": 0.30,
        "trailing_atr": 1.0,        # V8.2: 0.5→1.0 (匹配 1h ATR)
        "trailing_pct": 0.010,
        "hard_sl_atr": 1.0,
        "hard_sl_pct": 0.010,
        "time_stop_min": 75,
        "time_stop_min_pnl": 0.001, # V8.2: 0.3%→0.1%, 对齐 TP1 量级
        "cs_decay_threshold": 15,   # V8.2: 25→15, 适配实测 CS 范围
        "cs_decay_close_pct": 0.50,
        "weekly_review": False,
        "tighten_trailing_pct": 0.006,
        "tighten_trailing_atr": 0.6,  # V8.2: 0.3→0.6
        "liq_force_usd": 100_000_000,
        "spread_force_mult": 3.0,
    },
    "long_term": {
        "tp1_atr": 2.0,
        "tp1_pct": 0.05,
        "tp1_close_pct": 0.30,
        "tp2_atr": 4.0,
        "tp2_pct": 0.10,
        "tp2_close_pct": 0.30,
        "trailing_atr": 1.5,
        "trailing_pct": 0.03,
        "hard_sl_atr": 2.0,
        "hard_sl_pct": 0.04,
        "time_stop_min": None,
        "time_stop_min_pnl": None,
        "cs_decay_threshold": 15,   # V8.2: 25→15
        "cs_decay_close_pct": 0.50,
        "weekly_review": True,
        "tighten_trailing_pct": 0.015,
        "tighten_trailing_atr": 0.8,
        "liq_force_usd": 100_000_000,
        "spread_force_mult": 3.0,
    },
}

# 仓位调节 (在风险定仓之上再乘)
POSITION_CS_STRONG_MULT = 1.5
POSITION_CS_STRONG_THRESHOLD = 35   # V8.2: 原硬编码 60 → 35 (对齐 STRONG≈45)
POSITION_CONF_LOW_THRESHOLD = 0.6
POSITION_CONF_LOW_MULT = 0.7
POSITION_ATR_HIGH_MULT = 0.5
POSITION_ATR_HIGH_RATIO = 2.0
