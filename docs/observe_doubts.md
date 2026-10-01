# 观察疑问簿（赚钱目标）

> 半小时巡检自动追加；人工可继续批注。不宣称已验证 edge。

| 首次记录 | 级别 | 疑问 | 状态 |
|---|---|---|---|
| 09-16 11:28 | P0 | Binance评分源持续stale，标的价冻结却仍在打分——分数时效性存疑，赚钱链路实质断开 | open |
| 09-16 11:28 | P0 | enabled=true 但 tradable=false：状态看起来能交易，执行层不能赚钱 | open |
| 09-16 11:28 | P2 | 消息面 缺项：缺项: whale_institutional —— 客观性不足时仍参与加权？ | open |
| 09-16 11:28 | P2 | 数据面 缺项：缺项: mvrv, exchange_reserves —— 客观性不足时仍参与加权？ | open |
| 09-16 11:28 | P2 | 预测面 缺项：缺项: polymarket_prob, fedwatch_proxy —— 客观性不足时仍参与加权？ | open |
| 09-16 11:59 | P0 | 打分环疑似停转：decision_audit 最后一条停在 ~11:12，此后半小时无新audit；面板仍返回冻结快照（CS/ stale=9124 不变）。同时 Binance 返回 HTTP 451（地区限制），评分与衍生品采集受阻——赚钱链路不仅 tradable=false，连持续打分都断了 | open |
