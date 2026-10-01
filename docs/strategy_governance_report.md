# 策略配置版本治理 · 交付说明

**本任务完成只代表配置治理修复，不代表门槛合理、策略盈利或适合实盘。**

## 核实结论（修复前）

| 项 | 状态 |
|---|---|
| ACTIVE / 哈希覆盖 | 仅约 17 个顶层项（维度权重、部分阈值、ADX 调节） |
| 子权重 / 映射锚点 / 风控出场 / pretrade | **读代码常量**，未进版本 |
| `frozen` EffectiveConfig | 内部仍为可变 dict |
| 回滚 | 只能恢复顶层 flat，不能恢复完整配方 |
| 同名 v1 | 面板/引擎可对齐顶层，但子配方仍随代码漂移 |

## 修改文件

| 文件 | 作用 |
|---|---|
| `config/strategy_bundle.py` | 完整参数采集、深度不可变、parameters_hash、implementation_id、校验 |
| `config/strategy_store.py` | 版本落盘、原子激活/回滚、legacy_compose、迁移 |
| `config/effective_config.py` | 生产冻结改为加载 StrategyBundle |
| `mappers/*_mapper.py` | `strategy_params=` 本轮快照 |
| `runtime/live_loop.py` | 评分前钉死并传入 mapper |
| `engine/scorer.py` / `utils/scoring.py` | 使用密封开关/共线组 |
| `runtime/panel_server.py` | 展示 parameters_hash / implementation_id / strategy_identity |
| `scripts/migrate_strategy_bundle.py` | 生成迁移包（不切 ACTIVE） |
| `scripts/strategy_governance_verify.py` | 离线验收 |
| `tests/test_strategy_governance.py` | 验收用例 |
| `config/strategy_versions/sb_m1_current_baseline.json` | 迁移产物 |
| `docs/strategy_param_inventory.md` | 参数清单 |

## 新版本格式（摘要）

```json
{
  "schema_version": "strategy_bundle.v1",
  "strategy_version": "sb_m1_current_baseline",
  "parent_version": "v1",
  "created_at_ms": 0,
  "change_reason": "migration_from_partial_weights_ACTIVE",
  "parameters": { "...完整行为参数..." },
  "parameters_hash": "...",
  "implementation_id": "...",
  "strategy_identity": "...",
  "migration_note": "当前行为基线迁移…不能冒充历史 v1…",
  "unknown_historical_fields": ["sub_weights_at_original_v1_creation_time", "..."]
}
```

- `parameters_hash`：只覆盖 `parameters`（不含时间戳/备注）
- `implementation_id`：策略解释源码内容指纹（非单纯 Git HEAD）
- `strategy_identity`：`hash(parameters_hash|implementation_id)`

## 迁移规则

1. **原始** `config/weights_versions/v1.json` **保持不变**
2. 生成 `sb_m1_current_baseline`：顶层 = 当前 weights ACTIVE；其余 = 当前代码默认
3. 明确标记「当前行为基线迁移」+ `unknown_historical_fields`
4. **未**自动写入 `config/strategy_versions/ACTIVE.json`，也未改 weights ACTIVE

## 部署 / 激活 / 回滚（勿自动执行）

```bash
# 审查迁移包后，如需切换完整策略 ACTIVE：
python3 - <<'PY'
from config.strategy_store import activate_strategy
activate_strategy("sb_m1_current_baseline", reason="ops_review_ok")
PY

# 回滚到另一完整包：
# activate_strategy("sb_xxx", reason="rollback")

# 若 implementation_id 与当前代码不一致 → 激活被拒绝（需配套代码回滚或重新密封）
```

重启进程后才会加载新 ACTIVE。本任务不重启。

当前无 strategy ACTIVE 时：生产使用 `legacy_compose:<weights_ACTIVE>`（完整工厂⊕顶层覆盖），并带 migration_note，**不宣称历史完整恢复**。

## 离线验收

```bash
python3 -m scripts.strategy_governance_verify
```

## 最终验收栏

| 项 | 结论 |
|---|---|
| 配置治理离线 | 以 verify 脚本为准 |
| 测试网执行 | 未验证（本任务禁止下单） |
| 策略有效性 | 不适用 / 证据不足 |
| 实盘 | 不满足 |

## 未解决

- 无法从旧 v1 文件恢复创建时的子权重/映射/出场完整历史 → 已标 unknown
- 自动调参白名单仍为旧 `TUNABLE_PARAMS`（刻意不扩大 AI 可改范围）
- 采集器窗口状态在参数切换后的预热策略：已定义「决策边界切换」；有状态缓冲重建细则可后续增强
