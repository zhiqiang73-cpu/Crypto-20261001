# 第二轮强制验收报告

**日期**：2026-09-15  
**结论（三选一）**：**离线工程验收通过**；测试网验证待授权；策略有效性尚未验证。

> 本报告**不宣称**独立工程审查已通过，**不宣称**测试网已验证，**不宣称**策略可盈利。

---

## 1. 总结论

权限内可复现的验收失败（ORD / RISK / DATA / STRAT / LEDGER / CFG / GATE / COLL / POS / ALGO）在假交易所 + 离线单测路径上均已消失。全仓 `unittest discover`：**263 OK**。

仍阻塞：

1. 测试网 E2E（需显式授权后才能连接/下单）
2. 独立审查复查
3. 策略前向有效性 / 真实盈利
4. 真实 MVRV / 交易所储备等链上源

---

## 2. 逐 ID 矩阵

| ID | 原错误 | 修复要点 | 关键生产文件 | 测试 | 关键断言 | 结果 |
|----|--------|----------|--------------|------|----------|------|
| ORD-01 | 部分成交记意图量 | 确认仓=成交量 | `trading/position_manager.py` | ORD01 | qty==0.005 | 通过 |
| ORD-02 | 步长后仍记意图 | lot-step 截断写入；假盘 `_lot_step_size` | position_manager, fake_exchange | ORD02 | 非整手被截断 | 通过 |
| ORD-03 | NEW+UNKNOWN→ok | UNKNOWN 非 ok，禁止 qty 冒充 | `binance_client.py` | ORD03 | ok=False | 通过 |
| ORD-04 | 查询失败→REJECTED | 保持 UNKNOWN + 同 cid | binance_client | ORD04 | state≠REJECTED | 通过 |
| ORD-05 | 双 horizon 未走入口 | on_snapshot→净仓 | executor | ORD05 | 净 0.006 | 通过 |
| RISK-01 | 平仓拒后撤保护 | `release_protection_if_flat` | risk_guardian, executor | RISK01 | stops 仍在 | 通过 |
| RISK-02 | 保护成交不回写 | reconcile→force_close 账本 | risk_guardian | RISK02 | 本地 flat | 通过 |
| RISK-03 | 部分平后无保护 | 净仓单 Algo；新 ACK 再撤旧 | risk_guardian | RISK03 | stops 仍在 | 通过 |
| RISK-04 | 无 mark 仍 NORMAL | 启动 NOT_READY | risk_guardian | RISK04 | allow_new=False | 通过 |
| ALGO-01 | 旧 STOP_MARKET | `/fapi/v1/algoOrder` | binance_client, fake_exchange | ALGO01 | is_algo/CONDITIONAL | 离线通过；测试网阻塞 |
| DATA-01 | 年均价套 MVRV | 权重 mvrv=0；停用锚点 | data_mapper, weights | DATA01 | mvrv 权=0 | 通过 |
| DATA-02 | 动量当储备 | `price_momentum_24h` 独立键 | weights, mapper, panel | DATA02 | reserves 权=0 | 通过 |
| DATA-03 | 清算零值糊 | `liq_window_status` 真零/暖机/失败 | snapshots, mapper, collector | DATA03 | warmup∈missing | 通过 |
| STRAT-01 | 强信号 75U | 硬钳 ≤50 USDT | position_manager | STRAT01 | risk≤50 | 通过 |
| LEDGER-01 | 拒单当成交 | rejected / entry_kind | executor, trade_ledger | LEDGER01 | 无伪 fill | 通过 |
| LEDGER-02 | 重复累计 | fill 幂等键 | trade_ledger | LEDGER02 | len==1 | 通过 |
| LEDGER-03 | Guardian 漏账 | `executor.run_guardian`→`_record_action` | executor, panel | LEDGER03 | close∈ledger | 通过 |
| LEDGER-04 | 结算前视 | 丢弃 close>window_end 的 bar | settle.py | LEDGER04 | 非确定性 CORRECT | 通过 |
| CFG-01 | 记账热读 ACTIVE | sticky `config_snapshot` | executor, live_loop | CFG01 | version=v_decision | 通过 |
| GATE-01 | tradable 默认 True | 缺字段拒开 | executor | GATE01 | 无 open | 通过 |
| COLL-01 | 共线未算 | `_collinear_whale` 时封顶 | scoring.py | COLL01 | news+data≤0.35 | 通过 |
| POS-01 | 需正向开仓 | enabled+合法快照 | executor | POS01 | open.ok | 通过 |

指标登记表：[`docs/indicator_registry.md`](indicator_registry.md)。

---

## 3. 关键反例（修前 → 修后）

| 反例 | 修前 | 修后 |
|------|------|------|
| 请求 0.010 / 成交 0.005 | 本地 0.010 | 本地 0.005 |
| NEW + UNKNOWN | ok=True, qty=请求量 | ok=False，未决 |
| 平仓 reject | 保护被撤 | 保护保留 |
| 止损触发后价格反弹 | 账本仍有仓 / 可能重开意图 | 账本清空并对齐交易所 |
| 强信号定仓 | risk≈75 | risk≤50 |
| 拒单 | 写入成交账本 fee=0 | rejected / audit，fee_unknown |
| 00:30–01:30 + 01:00 bar high | 确定性 CORRECT | uncertain / invalid |
| 缺 tradable | getattr 默认 True 放行 | 拒绝开仓 |
| 旧 STOP_MARKET | 假盘接受 | Algo CONDITIONAL |

---

## 4. 真实调用链（离线）

```
采集(collectors) → 映射(mappers) → 评分(live_loop.score_once)
  → FactorSnapshot(tradable, config_snapshot, data_records)
  → TradeExecutor.on_snapshot
      → ExitChecker / manager.on_signal
      → FakeBinanceClient / BinanceTestnetClient
      → _record_action → TradeLedger (仅确认成交)
  → executor.run_guardian → RiskGuardian.tick
      → reconcile_exchange_fills / ensure_protection(Algo)
      → _record_action
```

评分超时/异常：`panel_server` → `guardian.note_score_result(False)`。

---

## 5. 已跑 / 未跑

**已跑（离线）**

- `python3 -m unittest discover -s tests` → 263 OK
- `python3 -m unittest tests.test_round2_acceptance` → 22 OK

**未跑（边界外）**

- 读取密钥 / 启动会下单的服务
- 测试网或实盘任何下单、撤单、改杠杆、改保证金
- 覆盖交易历史
- 独立审查官重测

---

## 6. 测试网步骤（获授权前不执行）

1. 确认环境变量与密钥权限仅限测试网；只读连通 `ping` / `exchangeInfo`
2. 最小名义开仓（短线）→ 确认 `executedQty` 与本地确认仓一致
3. 挂净仓 Algo 保护（`POST /fapi/v1/algoOrder`，`algoType=CONDITIONAL`）
4. 部分减仓 → 确认旧保护保留至新单 ACK 后替换
5. 全平 → 确认平仓成功后才撤保护
6. 进程重启 → 未决订单对账 → 受限运行（不对账成功不开新仓）
7. 故意触发一次评分超时 → Guardian 仍轮询且禁止新风险

每步记录：请求/响应原文、本地账本行、交易所净仓、Algo 状态。

---

## 7. 自我否定检查

| 检查项 | 结果 |
|--------|------|
| 是否仍把 UNKNOWN 当成功？ | 否（ORD03/04） |
| 是否仍把拒单记成交？ | 否（LEDGER01） |
| 是否平仓失败仍撤保护？ | 否（RISK01） |
| 是否用永久禁用自动交易绿测试？ | 否（POS01 enabled=True） |
| 是否结算用窗外价定对错？ | 否（LEDGER04） |
| 是否宣称测试网/独立审查通过？ | **否** |
| 权限内能否再复现记错仓/撤保护/伪收益？ | 当前离线套件未复现 |

若后续独立审查或测试网再打出反例，本结论自动降级为「离线部分通过」，继续修。

---

## 8. 交付物

- [`docs/round2_acceptance.md`](round2_acceptance.md) — 追踪表（状态已更新）
- [`docs/indicator_registry.md`](indicator_registry.md) — 指标登记
- [`tests/test_round2_acceptance.py`](../tests/test_round2_acceptance.py) — 22 项钉死断言
- 本报告
