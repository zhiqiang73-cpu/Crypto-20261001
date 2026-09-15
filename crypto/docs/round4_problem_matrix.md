# Round-4 问题矩阵

边界：工程一致性整改。不碰密钥、不下真实单、不改运行中 ACTIVE/账本。  
**工程通过 ≠ 策略有效 ≠ 可实盘。**

| ID | 问题 | 核实结果 | 根因 / 生产链 | 修复 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| P1 | 面板/引擎/执行阈值不一致 | **仍存在** | ACTIVE v1=±35·15/45/25/15；`weights.py` 出厂=±20·20/35/25/20；面板曾读模块常量；引擎 `use_active_overrides` 读 ACTIVE；快照 params 未驱动面板 | `config/effective_config.py` 冻结快照；`live_loop` 每轮 `apply_frozen_config`；`panel` 读 `config_snapshot.decision_thresholds`；HTML 禁止硬编码 20；落盘 `v2.json`（**未切 ACTIVE**） | `test_round4_acceptance.TestP1*` | **离线通过**；部署切 v2 **未执行** |
| P2 | 「不交易」原因不可审计 | **仍存在→已补** | 仅有 2h 稀疏观察点 | `review/decision_audit.py`；每轮评分写审计；区分 signal_state | `TestP2*` | **离线通过**；历史 8h 无法回填 |
| P3A | 巨鲸读错字段 | **仍存在** | `live_loop` 读 `news_result.indicator_scores`，真实为 `sub_scores` | 改为 `sub_scores` | `TestP3Whale*` | **通过** |
| P3B | 清算热力图当未来磁铁 | **仍存在** | `data_mapper._map_heatmap` + 权重 0.25 | 方向权重→0；映射恒 0；保留观测语义说明 | `TestP3Heatmap*` + data_mapper 测 | **通过**；独立预测验证 **未做** |
| P3C | 预测期限混用 | **部分** | Predict.fun 多窗 + Polymarket 月度 | 回放骨架要求元数据；完整转换模型 **未验证** | replay 入口 | **未验证／阻塞**（缺历史元数据） |
| P3D/E | 技术/消息/数学一致性 | **部分** | 范围大 | 本轮未全改；列入后续 | — | **部分未完成** |
| P4 | 旧信号追价 | **缺失→已补** | 开仓直接用评分 mark | `trading/pretrade.py`；executor 下单前复核 | `TestP4*` | **离线通过** |
| P5A | 价格出场独立 | **基本已有** | Guardian hard_sl | 回归测试确认；评分卡死仍可硬止损 | `TestP5*` | **离线通过** |
| P5B | 逐时点回放 | **缺数据** | 无当时可得四面轨迹 | `scripts/replay_entry.py` 价格路径骨架 | demo | **证据不足** |

## 强制结论栏

| 项 | 结论 |
|---|---|
| 工程一致性与离线可靠性 | **通过**（本轮新增/相关测例） |
| 测试网真实执行验证 | **未验证** |
| 策略样本外有效性 | **证据不足** |
| 实盘使用条件 | **不满足** |

## 部署注意（未擅自执行）

1. 观察/运行进程仍用 **ACTIVE=v1**。代码已让面板跟随快照；**重启进程后**面板门槛将显示 ±35（与引擎一致），不再显示错误的 ±20。
2. 若业务意图是 V8.2 的 ±20，部署时将 ACTIVE 切到 **v2**（已生成 `config/weights_versions/v2.json`），再重启。
3. 不自动重启 panel；不改 positions/ledger。
