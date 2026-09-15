"""BTC/USDT V7 双向统一评分体系配置"""

# V8: 短期 1h / 长期月级 — 消息+预测提权, 数据+技术降权
DIMENSION_WEIGHTS = {
    "long_term":  {"news": 0.35, "data": 0.25, "tech": 0.10, "prediction": 0.30},
    "short_term": {"news": 0.20, "data": 0.35, "tech": 0.25, "prediction": 0.20},
}

# V8.2 决策阈值 — 对齐实测 CS 分布 (±15~30), 使 STANDARD 可触发
DECISION_THRESHOLDS = {
    "strong_long":    45,   # CS >= +45
    "standard_long":  20,   # +20 <= CS < +45
    "watch_long":      8,   # +8  <= CS < +20
    "neutral_upper":   8,   # |CS| < 8 = 中性
    "neutral_lower":  -8,
    "watch_short":    -8,   # -20 < CS <= -8
    "standard_short":-20,   # -45 < CS <= -20
    "strong_short":  -45,   # CS <= -45
}

# 安全阀: 任一维度与CS方向矛盾超过此值则降级
SAFETY_VALVE_THRESHOLD = 50

# 消息面子权重 — 短/长期差异化情报源
# 短期: 突发/whale/监管事件/宏观意外/黑天鹅
# 长期: 宏观消化 + ETF + 减半等
NEWS_SUB_WEIGHTS = {
    "long_term": {
        "monetary_policy": 0.25,
        "etf_flows": 0.20,
        "regulations": 0.15,
        "macro_data": 0.18,
        "institutional_gov": 0.08,
        "halving": 0.05,
        "usdt_dynamics": 0.05,
        "black_swan": 0.04,
    },
    "short_term_normal": {
        "breaking_crypto": 0.35,
        "whale_institutional": 0.25,
        "regulatory_event": 0.15,
        "macro_surprise": 0.15,
        "black_swan": 0.10,
    },
}

# 短期数据面全局 35%: 层内比例保持 onchain 低 / derivatives 高 / micro 中
DATA_LAYER_WEIGHTS = {
    "long_term":  {"onchain": 20/25, "derivatives": 3/25, "microstructure": 2/25},
    "short_term": {"onchain": 5/35, "derivatives": 20/35, "microstructure": 10/35},
}

ONCHAIN_INDICATOR_WEIGHTS = {
    # 长期: exchange_reserves 仅为 24h 动量代理 → 降权; 重分到 mvrv/whale/hashrate
    # mvrv 权重 0: 无真实 MVRV，年均价代理不得冒充估值因子
    "long_term":  {"exchange_reserves": 0.0, "mvrv": 0.0, "price_momentum_24h": 0.15,
                   "lth_supply_ratio": 0.0,
                   "nupl": 0.0, "miner_reserves": 0.0, "usdt_market_cap": 0.0,
                   "sopr": 0.0, "whale_transfers": 0.45, "hashrate": 0.40},
    "short_term": {"whale_transfers": 1.0, "exchange_reserves": 0.0,
                   "mvrv": 0, "price_momentum_24h": 0.0,
                   "lth_supply_ratio": 0, "nupl": 0,
                   "miner_reserves": 0, "usdt_market_cap": 0, "sopr": 0, "hashrate": 0}
}

DERIVATIVES_INDICATOR_WEIGHTS = {
    "long_term":  {"implied_volatility": 0.35, "open_interest": 0.20, "funding_rate": 0.15,
                   "option_max_pain": 0.15, "long_short_ratio": 0.05,
                   "liquidation_heatmap": 0.05, "cvd": 0.05, "liquidations_realtime": 0},
    "short_term": {"funding_rate": 0.25, "liquidation_heatmap": 0.25, "open_interest": 0.20,
                   "cvd": 0.15, "liquidations_realtime": 0.08, "long_short_ratio": 0.03,
                   "option_max_pain": 0.02, "implied_volatility": 0.02}
}

MICROSTRUCTURE_INDICATOR_WEIGHTS = {
    "long_term":  {"block_trades": 0.40, "session_liquidity": 0.40,
                   "orderbook_depth": 0.10, "spread": 0.10,
                   "liquidation_speed": 0, "oi_velocity": 0},
    "short_term": {"orderbook_depth": 0.28, "liquidation_speed": 0.25, "block_trades": 0.20,
                   "oi_velocity": 0.12, "spread": 0.08, "session_liquidity": 0.07}
}

# 技术面子指标权重 (按手册合理性与短期可用性分配, ADX/布林带为调节因子不占权重)
TECH_INDICATOR_WEIGHTS = {
    # V8.2: EMA/VWAP 与 structure 共线 → 降权; MACD/RSI_div 提权
    "long_term": {
        "market_structure": 0.16, "ema_stack": 0.07, "volume_profile": 0.12,
        "support_resistance": 0.10, "vwap": 0.04, "pdh_pdl": 0.06,
        "rsi_divergence": 0.13, "macd_hist": 0.12, "volume": 0.08, "obv": 0.04,
        "pin_bar": 0.04, "engulfing": 0.02, "inside_bar": 0.02,
        "fibonacci": 0.0, "order_blocks": 0.0, "fvg": 0.0,
    },
    "short_term": {
        "market_structure": 0.18, "ema_stack": 0.06, "volume_profile": 0.10,
        "support_resistance": 0.08, "vwap": 0.05, "pdh_pdl": 0.08,
        "rsi_divergence": 0.12, "macd_hist": 0.10, "volume": 0.10, "obv": 0.03,
        "pin_bar": 0.05, "engulfing": 0.03, "inside_bar": 0.02,
        "fibonacci": 0.0, "order_blocks": 0.0, "fvg": 0.0,
    },
}

# ADX / 布林带挤压调节
ADX_STRONG = 40.0
ADX_WEAK = 20.0
ADX_BOOST = 1.2
ADX_DAMPEN = 0.6
BOLL_SQUEEZE_PERCENTILE = 0.10   # Bandwidth 处于近 20 根最低 10% → 挤压
BOLL_SQUEEZE_BOOST = 1.15

# 预测面子指标权重
# V3: Predict.fun(币安预测) + Polymarket + 情绪/期权 三角共识
PREDICTION_SUB_WEIGHTS = {
    "long_term": {
        "predict_fun_btc": 0.25,   # 币安预测市场日/小时方向
        "polymarket_prob": 0.30,   # Polymarket 月度阈值
        "prob_change_speed": 0.0,  # 无可靠 market_id/期限模型 → 停用交易因子，仅观察
        "fedwatch_proxy": 0.15,
        "fear_greed": 0.10,
        "max_pain": 0.10,
    },
    "short_term": {
        "predict_fun_btc": 0.45,   # 币安预测市场短窗 Up/Down (主)
        "polymarket_prob": 0.20,   # Polymarket 方向参考
        "fedwatch_proxy": 0.10,
        "fear_greed": 0.10,
        "max_pain": 0.15,
    },
}

# ---------------------------------------------------------------------------
# V8.3 工程重构: 实验开关与共线标注
# ---------------------------------------------------------------------------
# agreement_boost 默认关闭 — 先压少数派再奖多数派会放大同质信息
ENABLE_AGREEMENT_BOOST = False
AGREEMENT_BOOST_LO = 0.7
AGREEMENT_BOOST_HI = 1.3

# 已知共线 / 信息重复组 — 合并贡献上限 (相对维度内权重和)
COLLINEAR_GROUPS = {
    # 巨鲸: news.whale_institutional ↔ data.whale_transfers
    "whale": {
        "faces": ("news", "data"),
        "news_keys": ("whale_institutional",),
        "data_keys": ("whale_transfers",),
        "max_combined_face_contrib": 0.35,  # 两面合计对 CS 的贡献上限 (提示级)
    },
    # 结构/EMA/VWAP: V8.2 已降权
    "structure_ema_vwap": {
        "tech_keys": ("market_structure", "ema_stack", "vwap"),
        "note": "V8.2 已降权, 保留观察",
    },
}

# 代理指标诚实化映射 (旧名 → 新名); 权重键仍用旧名以兼容版本文件,
# 但 UI/日志应显示 proxy_label
PROXY_INDICATOR_LABELS = {
    "mvrv": "price_to_365d_avg (proxy, not real MVRV)",
    "exchange_reserves": "price_momentum_24h (proxy, not exchange reserves)",
}

ENABLE_CONSISTENCY_DAMPING = False  # 无独立验证不默认改反对意见
