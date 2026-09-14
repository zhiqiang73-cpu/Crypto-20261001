"""BTC/USDT V7 双向统一评分体系配置"""

# V8: 短期 1h / 长期月级 — 消息+预测提权, 数据+技术降权
DIMENSION_WEIGHTS = {
    "long_term":  {"news": 0.35, "data": 0.25, "tech": 0.10, "prediction": 0.30},
    "short_term": {"news": 0.20, "data": 0.35, "tech": 0.25, "prediction": 0.20},
}

# V7 决策阈值 (多空不对称, 基于 [-100, +100])
DECISION_THRESHOLDS = {
    "strong_long":    60,   # CS >= +60
    "standard_long":  35,   # +35 <= CS < +60
    "watch_long":     10,   # +10 <= CS < +35
    "neutral_upper":  10,   # |CS| < 10 = 中性
    "neutral_lower": -10,
    "watch_short":   -10,   # -40 < CS <= -10
    "standard_short":-40,   # -65 < CS <= -40
    "strong_short":  -65,   # CS <= -65
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
    # 长期: 无免费源的指标权重归零, 重分到可代理项 (reserves~/mvrv~/whale/hashrate)
    "long_term":  {"exchange_reserves": 0.35, "mvrv": 0.35, "lth_supply_ratio": 0.0,
                   "nupl": 0.0, "miner_reserves": 0.0, "usdt_market_cap": 0.0,
                   "sopr": 0.0, "whale_transfers": 0.18, "hashrate": 0.12},
    "short_term": {"whale_transfers": 0.90, "exchange_reserves": 0.10,
                   "mvrv": 0, "lth_supply_ratio": 0, "nupl": 0,
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
    "long_term": {
        "market_structure": 0.16, "ema_stack": 0.14, "volume_profile": 0.12,
        "support_resistance": 0.10, "vwap": 0.08, "pdh_pdl": 0.06,
        "rsi_divergence": 0.08, "macd_hist": 0.06, "volume": 0.08, "obv": 0.04,
        "pin_bar": 0.04, "engulfing": 0.02, "inside_bar": 0.02,
        "fibonacci": 0.0, "order_blocks": 0.0, "fvg": 0.0,
    },
    "short_term": {
        "market_structure": 0.18, "ema_stack": 0.12, "volume_profile": 0.10,
        "support_resistance": 0.08, "vwap": 0.10, "pdh_pdl": 0.08,
        "rsi_divergence": 0.08, "macd_hist": 0.05, "volume": 0.08, "obv": 0.03,
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
        "prob_change_speed": 0.10,
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
