# 第三轮强制验收追踪表

基线：2026-09-15 第三轮。保留前两轮未提交改动。
状态：未检查 | 已复现 | 修复中 | 离线验收通过 | 外部依赖阻塞

## 代码基线

- 路径：`/Users/zengyun/Downloads/我的AI/crypto`
- 默认：`enabled=False`；长期观察-only
- 实际衍生品采集：**FreeDerivativesCollector**
- 评分→交易：`panel_server` → `score_once` → `on_snapshot`
- Guardian：`executor.run_guardian` → `tick`

## 生产调用图

见 `docs/round3_report.md` 与源码：开/平/减仓 → `PositionManager` → `_sync_exchange_to_net`；对账 → `apply_external_position_update`（不下单）；记账 → `_record_action`；费用 → `fee_adapter`。

| ID | 业务不变量 | 生产入口 | 失败复现 | 根因 | 修复位置 | 行为测试 | 故障测试 | 证据 | 状态 |
|----|------------|----------|----------|------|----------|----------|----------|------|------|
| EXEC-01 | 减仓确认=成交 | partial_close | EXEC01 | 意图记账 | position_manager | EXEC01 | exit_incomplete | round3 | 离线验收通过 |
| EXEC-02 | 全平半成交留残余 | _local_close | EXEC02 | ok清仓 | position_manager | EXEC02 | 保护保留 | round3 | 离线验收通过 |
| RECON-01 | 在途不对账下单 | reconcile | RECON01 | 无视inflight | coordinator字段 | RECON01 | 屏障 | round3 | 离线验收通过 |
| RECON-02 | 对账不交易 | reconcile | RECON02 | force_close副作用 | apply_external | RECON02 | 双策略 | round3 | 离线验收通过 |
| LEDGER-R3 | 成交幂等无接收时间 | ledger | LEDGER_R3 | ts_ms去重 | trade_ledger | LEDGER_R3 | 异tid同qty | round3 | 离线验收通过 |
| PROTECT-R3 | 触发后账本一致 | tick | PROTECT_R3 | — | guardian | PROTECT_R3 | 反弹 | round3 | 离线验收通过 |
| DATA-FREE | 质量进Free路径 | free.fetch | DATA_FREE | 只改coinglass | free_derivatives | DATA_FREE | 序列化 | round3 | 离线验收通过 |
| COLL-PROD | 共线生产生效 | score_once | COLL_PROD | 无检测 | detect_collinear | COLL_PROD | 真路径 | round3 | 离线验收通过 |
| CFG-R3 | 决策配置=计算配置 | score_once | — | 事后盖章 | live_loop钉死 | CFG01回归 | 热更 | 实现 | 离线验收通过 |
| FEE-01 | 费用适配器 | fee_adapter | FEE01 | 仅unknown | fee_adapter | FEE01 | 样本 | round3 | 离线验收通过 |
| RISK-PNL | 日内风险接通 | note_daily_pnl | RISK_PNL | 无人调用 | executor | RISK_PNL | REDUCE_ONLY | round3 | 离线验收通过 |
| CRASH-01 | 在途持久化 | persist | CRASH01 | 无inflight | position_manager | CRASH01 | reload | round3 | 离线验收通过 |

## 外部阻塞

| 项 | 依赖 |
|----|------|
| 测试网 E2E | 用户授权 |
| 策略有效性 | 前向样本 |
| 独立审查复查 | 另约 |

## 回归

- `python3 -m unittest discover -s tests` → **275 OK**
- `python3 scripts/round3_verify.py` → **PASS**
