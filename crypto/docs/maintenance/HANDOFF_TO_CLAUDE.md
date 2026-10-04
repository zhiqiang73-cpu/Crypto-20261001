# 给 Claude 的交接 · 2026-10-04 11:06 CST

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
- **`MARGIN_BUDGET_PER_TRADE` 仍为 0.35**（未改）
- **现有测试网仓位未动**；用户明确说 **先别重启** trader。因此进程内存里可能仍是旧 `RISK_R`，**重启 `python -m shadow.deploy` 后新开/新加层才按 0.01 生效**。

## 相关测试

已跑过并通过：`tests.test_strategy_drift_guard`、`tests.test_cross_only_strategy`、`tests.test_dual_strategy_books`。

## 背景备忘

- 项目：`/Users/zengyun/我的AI/crypto`
- 维护清单：`docs/maintenance/BUG_TRACKER.md`
- 用户偏好方向：逐步加仓 → 保本先平一部分 → 剩余追逐利润（后两段**尚未实现**；本次只落地单层改小）
- 安全边界：仅 paper/testnet，禁止主网

## 请你接手时注意

1. 不要把 `RISK_R` 写回 0.03，除非用户重新拍板。
2. 改策略实现相关文件后若触及 reseal 范围，按项目既有 `reseal_strategy` 流程走；本次改的是 `shadow/engine` + 策略卡，漂移哨兵期望与 `RISK_R=0.01` 一致。
3. 用户未授权重启前，不要自行重启 trader / 不要动 `runtime/` 仓位。
