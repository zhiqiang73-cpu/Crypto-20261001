# Round-4 修复说明与交付

## 结论（分栏，禁止合并措辞）

- **工程一致性与离线可靠性：通过**
- **测试网真实执行验证：未验证**
- **策略样本外有效性：证据不足**
- **实盘使用条件：不满足**

验收入口：`python3 -m scripts.round4_verify`  
结果样例：`docs/round4_verify_result.json`

---

## 1. 五类问题与代码位置

### P1 单一配置快照
- `config/effective_config.py` — `freeze_effective_config` / `apply_config_to_engine`
- `engine/scorer.py` — 禁止静默回退出厂继续交易；支持本轮 thresholds/weights
- `runtime/live_loop.py` — 评分前冻结并写入 `config_snapshot`
- `runtime/panel_server.py` — thresholds/权重来自快照
- `btc-four-face-monitor.html` — 门槛不再硬编码 20
- `config/weights_versions/v2.json` — 与当前 `weights.py` 对齐（**ACTIVE 仍为 v1**）

### P2 决策审计
- `review/decision_audit.py`
- `runtime/live_loop.py` 每轮 append；审计写失败则 `tradable=False`

### P3 语义
- 巨鲸：`live_loop` → `news_result.sub_scores`
- 清算热力图：权重 0 + mapper 恒 0（`config/weights.py`, `mappers/data_mapper.py`）

### P4 成交前复核
- `trading/pretrade.py`
- `trading/executor.py` 开仓前 `recheck_entry`

### P5 出场 / 回放
- Guardian hard_sl 回归测
- `scripts/replay_entry.py`（仅价格路径；四面回放数据不足）

---

## 2. 反例（修复前 → 后）

| 反例 | 修复前 | 修复后 |
|---|---|---|
| CS=21 面板显示 STANDARD，引擎 ACTIVE 要 35 | 面板读出厂 ±20 | 面板读快照；ACTIVE=v1 时显示 ±35 |
| 巨鲸 `indicator_scores` | 永远空，去重失效 | 读 `sub_scores` |
| 清算磁铁 | 方向权重 0.25 | 权重 0，分=0 |
| 追价 0.6 ATR | 仍可开 | pretrade 拒绝 |

---

## 3. 数据 / 配置 / 状态契约（摘要）

- **配置**：出厂 `weights.py` ⊕ ACTIVE → 不可变快照；一轮内禁止混版本。
- **full_cs**：只表示四面结构完整 ≠ 可交易。
- **signal_state**：`no_signal` / `data_unfit` / `risk_blocked` / `signal_ok` / …
- **仓位**：本地双 horizon；交易所净仓（维持现状）。

---

## 4. 回滚

- 代码：git revert 本批改动。
- 配置：ACTIVE 保持 v1 即仍为旧阈值；切 v2 后可用 `overrides.rollback("v1")`。
- **未部署 / 未重启运行中 panel。**

---

## 5. 剩余外部验证

- 测试网真实下单与保护单
- 预测市场历史元数据与期限转换
- 样本外扣费后表现
- 将 ACTIVE→v2 的业务确认（若要以 ±20 为生产意图）
