# 指标登记表（第二轮）

| 指标键 | 来源 | 单位 | 窗口 | 有效条件 | 交易权重 | 备注 |
|--------|------|------|------|----------|----------|------|
| mark_price | Binance WS/REST | USDT | 实时 | event_time 新鲜 <60s | 准入必需 | 无 mark → NOT_READY |
| atr | Klines 1h | 价格 | 14 | atr>0 | 定仓必需 | 无 ATR 拒绝开仓 |
| funding_rate | Binance | 小数/8h | 当期 | 非 NaN | short 衍生品层 | |
| whale_transfers | 免费链上代理 | BTC | 滚动 | 有方向证据 | short onchain 主 | 与 news 巨鲸共线封顶 |
| whale_institutional | 新闻 | 事件分 | 短窗 | 去重后 | news 子权 | COLLINEAR whale |
| price_to_365d_avg | blockchain.info 均价 | 比值 | 365d | 仅观察 | **mvrv=0** | 禁止套 MVRV_ANCHORS |
| price_momentum_24h | Binance 24h | [-1,1] | 24h | 非储备 | long 独立键 0.15 | 原 exchange_reserves 代理 |
| exchange_reserves | — | — | — | 无可靠源 | **0** | 已停用交易 |
| mvrv | — | — | — | 无真实源 | **0** | 待真实数据 |
| polymarket btc_prob | Gamma | [0,1] | 至 expiry | 同 market_id | 预测面 | 禁止跨 id 算增速 |
| predict_fun | Predict.fun | [0,1] | 短窗 | 窗口匹配 horizon | 预测面 | 短长期不得混窗 |
| liq_5m_usd | 衍生品 | USD | 5m | 完整窗口真零可 VALID | 黑天鹅/出场 | 暖机/断流 ≠ 0 |
| fear_greed | alternative.me | 0-100 | 日 | 有时间戳 | 预测子权 | |

## DataRecord 生产接入点

- 定义：`models/data_record.py`
- 准入：`TradeExecutor.on_snapshot` 检查 `tradable`（缺字段拒绝）+ `staleness_sec` + Guardian `allow_new_entries`
- 评分：`LiveScoringLoop.score_once` 产出 `ScoringOutput` + `config_snapshot`
- 映射：`mappers/data_mapper.py` 不再把年均价送入 MVRV_ANCHORS

## 明确未验证

- 真实 MVRV / 交易所储备：外部数据阻塞
- 策略因子有效性：需前向样本
