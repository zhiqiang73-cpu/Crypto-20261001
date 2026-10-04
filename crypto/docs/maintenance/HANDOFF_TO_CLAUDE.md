# 给 Claude 的交接 · 2026-10-04 13:40 CST

来源：本机助手「Crypto调试助手」（Grok Bot），用户要求把改动通知你。

## 刚做完、尚未重启

- **`RISK_R`：0.03 → 0.01**（单层约变为原来的 1/3，方便最多 3 层逐步加仓）
- **已改文件（请一并视为权威）：**
  - `shadow/engine.py`（常量 + 注释表「现用」标记）
  - `config/strategies/deployed_kdj_extreme_v1.json`
  - `config/strategies/deployed_kdj_5m_extreme_v1.json`
  - `config/strategies/deployed_kdj_eth_extreme_v1.json`
  - `config/strategies/deployed_kdj_eth_5m_extreme_v1.json`
  - `docs/strategy_governance.v1.json` → `risk.risk_r = 0.01`
  - 测试：`tests/test_strategy_drift_guard.py`、`tests/test_cross_only_strategy.py`
- **`MARGIN_BUDGET_PER_TRADE`：0.35 → 0.70**（BTC/ETH 两个标的统一）
- **新增 `PORTFOLIO_MARGIN_BUDGET = 0.80`**：BTC+ETH 合计保证金 ≤ 权益 80%，
  先到先得。单标的 0.70 保留为次级上限，实际取两者更紧的一个。
  例：BTC 先建满 3 层占 68% → ETH 只能用剩余 12%（≈0.28 层量）。
- **`place_limit_chase` 不再「部分成交即结束」**（2026-10-04 修复）：
  被动挂单吃到一部分后，会把剩余量继续挂到窗口耗尽，再只对剩余量做 IOC 兜底。
  事故原状：目标 0.2010 BTC 只成交 0.0048（2.4%），被动尝试 1 次就收工。
  同时修好部分成交的均价回填（`cumQuote / executedQty`，此前恒为 0）。
  chase meta 新增 `maker_filled` / `taker_filled`，用于算真实加权费率。
- **BTC/ETH 15m 分层风险**：第1层 `r=0.01`；第2/3层各 `r=0.005`
- **5m 策略保持单层旧逻辑**，未受分层风险表影响
- **现有测试网仓位未动**；用户明确说 **先别重启** trader。因此进程内存里可能仍是旧 `RISK_R`，**重启 `python -m shadow.deploy` 后新开/新加层才按 0.01 生效**。

## 相关测试

已跑过并通过：`tests.test_strategy_drift_guard`、`tests.test_cross_only_strategy`、`tests.test_dual_strategy_books`。

## 背景备忘

- 项目：`/Users/zengyun/我的AI/crypto`
- 维护清单：`docs/maintenance/BUG_TRACKER.md`
- 用户偏好方向：逐步加仓 → 保本先平一部分 → 剩余追逐利润（分层风险已落地；分段减仓仍未实现）
- 安全边界：仅 paper/testnet，禁止主网

## 请你接手时注意

1. 不要把 `RISK_R` 写回 0.03，除非用户重新拍板。
2. 改策略实现相关文件后若触及 reseal 范围，按项目既有 `reseal_strategy` 流程走；本次改的是 `shadow/engine` + 策略卡，漂移哨兵期望与 `RISK_R=0.01` 一致。
3. 用户未授权重启前，不要自行重启 trader / 不要动 `runtime/` 仓位。
