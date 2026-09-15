# 第二轮强制验收追踪表

基线：2026-09-15 第二轮开始前。保留上一轮未提交改动，不回滚。
状态枚举：未检查 | 已复现 | 修复中 | 离线验收通过 | 外部验证阻塞

| ID | 问题 | 生产入口 | 失败复现 | 根因 | 修复位置 | 回归测试 | 故障测试 | 状态 |
|----|------|----------|----------|------|----------|----------|----------|------|
| ORD-01 | 部分成交本地记满意图量 | position_manager._local_open / market_open | test_ORD01_partial_fill | 按意图记账，忽略 filled | trading/position_manager.py, models.py | ORD01 | 部分成交后崩溃恢复 | 离线验收通过 |
| ORD-02 | 步长取整后账本仍记意图 | _calc_qty + _local_open | test_ORD02_lot_step_rounding | 未用 submitted 量更新账本；假盘方法名遮蔽 | position_manager.py, fake_exchange.py | ORD02 | 小额减仓/残余 | 离线验收通过 |
| ORD-03 | NEW+查询 UNKNOWN 仍 ok=True | binance_client.market_open | test_ORD03_new_then_unknown | qty_filled or qty + ok=True | binance_client.py | ORD03 | 契约解析样本 | 离线验收通过 |
| ORD-04 | 超时查询失败标 REJECTED | market_open except | test_ORD04_timeout_query_fail | 查询失败→REJECTED | binance_client.py | ORD04 | 后续查询恢复 | 离线验收通过 |
| ORD-05 | 双策略净额从生产入口 | executor.on_snapshot | test_ORD05_dual_horizon_via_executor | 仅手写账本测过 | position_manager + executor | ORD05 | 并发意图 | 离线验收通过 |
| RISK-01 | 平仓拒绝后撤保护 | guardian.tick / executor._run_exits | test_RISK01_close_reject_keeps_stop | 不论成败 cancel_protection | risk_guardian.py, executor.py | RISK01 | 手动/紧急/反手路径 | 离线验收通过 |
| RISK-02 | 保护成交后价格反弹未同步 | guardian.tick | test_RISK02_stop_fill_then_bounce | 无保护成交回写 | risk_guardian.py | RISK02 | 乱序回调 | 离线验收通过 |
| RISK-03 | 部分平仓后剩余无保护 | ensure_protection | test_RISK03_partial_keeps_protection | 先撤后挂；虚拟量各挂 | risk_guardian.py | RISK03 | 新单失败 | 离线验收通过 |
| RISK-04 | 无行情仍 NORMAL 可开仓 | RiskGuardian.__init__ / _check_staleness | test_RISK04_not_ready_without_mark | last_mark_ts=0 跳过；默认 NORMAL | risk_guardian.py | RISK04 | 评分失败通知 | 离线验收通过 |
| ALGO-01 | 仍用旧 STOP_MARKET 端点 | place_stop_market | test_ALGO01_place_stop_uses_algo_fields | POST /fapi/v1/order | binance_client.py, fake_exchange.py | ALGO01 | 官方样本契约 | 离线验收通过（测试网实测阻塞） |
| DATA-01 | 年均价仍套 MVRV 锚点 | data_mapper | test_DATA01_price_avg_not_mvrv | MVRV_ANCHORS + 权重 0.45 | data_mapper, weights | DATA01 | 现价=年均→非+90 | 离线验收通过 |
| DATA-02 | 24h 动量仍当储备 | weights/mapper | test_DATA02_momentum_not_reserves | 键名 exchange_reserves | weights, mapping, panel | DATA02 | 面板文案 | 离线验收通过 |
| DATA-03 | 清算零值语义 | collectors/mapper | test_DATA03_liq_zero_semantics | 有价格即成功 | collectors, data_mapper, snapshots | DATA03 | 暖机/断流 | 离线验收通过 |
| STRAT-01 | 强信号风险 75>50 | _calc_qty | test_STRAT01_risk_cap_50 | risk_cap * 1.5 | position_manager.py | STRAT01 | 最小名义突破拒绝 | 离线验收通过 |
| LEDGER-01 | 拒单写真实成交 | executor._record_action | test_LEDGER01_reject_not_trade | 不看 order.ok | executor, trade_ledger | LEDGER01 | 审计事件 | 离线验收通过 |
| LEDGER-02 | 部分成交重复累计 | ledger | test_LEDGER02_partial_idempotent | 无幂等键 | trade_ledger | LEDGER02 | 乱序 | 离线验收通过 |
| LEDGER-03 | 出场路径漏记账 | guardian/force_close | test_LEDGER03_all_exits_ledger | 部分只写 _actions | executor.run_guardian | LEDGER03 | 手动/紧急 | 离线验收通过 |
| LEDGER-04 | 00:30–01:30 用窗外价 | settle_record | test_LEDGER04_window_no_lookahead | 整根 1h bar | settle.py | LEDGER04 | ATR 无前视 | 离线验收通过 |
| CFG-01 | 记账再读 ACTIVE 版本 | _record_action | test_CFG01_config_snapshot_sticky | current_version_label() 热读 | executor, live_loop | CFG01 | 中途切换版本 | 离线验收通过 |
| GATE-01 | tradable 默认 True 放行 | on_snapshot | test_GATE01_missing_tradable_blocks | getattr(..., True) | executor, live_loop | GATE01 | 缺字段快照 | 离线验收通过 |
| COLL-01 | 共线组未真正计算 | scoring | test_COLL01_whale_cap | 仅配置注释 | utils/scoring.py | COLL01 | breakdown | 离线验收通过 |
| POS-01 | 合法短线正向开仓 | on_snapshot enabled | test_POS01_valid_short_opens | 需正向路径 | executor | POS01 | 长期观察 only | 离线验收通过 |

## 明确阻塞（非本轮“完成”）

| 项 | 状态 |
|----|------|
| 独立工程审查复查 | 外部验证阻塞 |
| 测试网 E2E 下单 | 外部验证阻塞（待授权） |
| 策略有效性/真实盈利 | 外部验证阻塞（需前向样本） |
| 真实 MVRV 数据源 | 外部验证阻塞 → 本轮降权为观察 |

## 未提交改动基线（保留）

上一轮工程重构：trading/*, models/data_record.py, review/trade_ledger.py, config/strategy_contract.py, tests/test_portfolio_manager.py, tests/test_engineering_refactor.py 等。本轮在其上修复，不回滚。

## 离线回归

- `python3 -m unittest discover -s tests` → **263 OK**（2026-09-15）
- 专项：`tests.test_round2_acceptance` → **22 OK**
