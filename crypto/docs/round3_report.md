# 第三轮强制验收报告

**日期**：2026-09-15  
**结论**：**离线工程正确性 — 通过**（在已知确定性反例与相邻故障场景下）。  
**测试网**：未执行（待授权）。  
**长期策略**：仍观察-only。  
**策略有效性**：未验证。  
**默认运行**：`TradeExecutor.enabled=False`，不自动下单。

> 不宣称独立审查必然通过；不宣称测试网已验证；不宣称可盈利。

---

## A. 总结论

| 维度 | 状态 |
|------|------|
| 离线工程正确性 | **通过**（`275` unittest OK；`scripts/round3_verify.py` PASS） |
| 真实测试网 | **未执行 / 待授权** |
| 长期自动交易 | **观察态** |
| 策略有效性 | **未验证** |
| 默认交易开关 | **关闭** |

本轮相对前两轮报告的态度：前两轮“通过”标签不足以采信；本轮以可复现反例（部分减仓/部分全平/并发对账/重复记账/免费采集器质量）为完成证据。

---

## B. 修复矩阵

| ID | 修前 | 根因 | 修后 | 位置 | 测试 | 结果 |
|----|------|------|------|------|------|------|
| EXEC-01 | 减仓记意图，本地0.005/交易所0.008 | 意图先改本地 | 确认=成交，残余0.008+exit_incomplete | `position_manager._partial_close_locked` | EXEC01 | OK |
| EXEC-02 | 全平半成交本地归零 | ok 即清仓 | 保留残余0.005 | `_local_close` | EXEC02 | OK |
| RECON-01 | 在途开仓被对账打平 | 无视在途 | inflight 跳过对账 | `register_inflight` + reconcile | RECON01 | OK |
| RECON-02 | 对账 force_close 额外下单 | 复用交易平仓 | `apply_external_position_update` 仅记账 | `risk_guardian` | RECON02 | OK |
| LEDGER-R3 | 同成交不同接收时间记两次 | 去重含 ts_ms | exchange_trade_id / 累计水位 | `trade_ledger` | LEDGER_R3 | OK |
| PROTECT-R3 | （回归）触发后反弹 | 对账下单风险 | 外部平仓本地清、无新单 | guardian | PROTECT_R3 | OK |
| DATA-FREE | 质量只改 coinglass | live 用 FreeDerivatives | `liq_window_status` 复制；暖机不刷新 available | `free_derivatives` | DATA_FREE | OK |
| COLL-PROD | 仅测试塞标志 | 生产未检测 | `detect_collinear_whale`→face_conf | scoring+live_loop | COLL_PROD | OK |
| CFG-R3 | 评分后 effective_params 盖章 | 事后热读 | 决策开始前钉死 snapshot | `live_loop` | （实现+CFG01回归） | OK |
| FEE-01 | 仅 fee_unknown | 无适配器 | `fee_adapter` + 官方样本 + fake userTrades | `trading/fee_adapter.py` | FEE01 | OK |
| RISK-PNL | note_daily_pnl 无人调用 | 死代码 | ledger→`_update_daily_pnl_from_ledger` | executor | RISK_PNL | OK |
| CRASH-01 | 在途不持久 | 无 inflight 落盘 | inflight_orders 持久化 | position_manager | CRASH01 | OK |
| ALGO | 假盘 is_algo 冒充 | 缺字段契约 | algoType/triggerPrice | fake+client | ALGO_Contract | OK |
| PRED | prob_change 无期限模型 | 跨窗语义 | long `prob_change_speed=0` | weights | — | 已停用 |
| MARK-TIME | now 冒充事件时间 | 兜底 now | 无 event_time→INVALID | live_loop | — | 已修 |
| MANUAL | 手工平仓不进账本 | 只写 history | `_record_action` + 保留保护 | executor.force_close | — | 已修 |

---

## C. 关键反例前后对比

1. **部分减仓**：意图0.005/成交0.002 → 前：本地0.005；后：本地=交易所=0.008 且 exit_incomplete  
2. **部分全平**：前：本地0；后：本地0.005  
3. **开仓与对账并发**：前：买完再卖空；后：inflight 时对账零下单  
4. **双策略残留+交易所空**：前：开空再买平；后：仅本地 EXTERNAL_FLAT，market_orders 不变  
5. **同成交不同接收时间**：前：2条；后：1条  
6. **保护触发反弹**：本地清空、无额外市价单  
7. **FreeDerivatives 暖机**：warmup→mapper missing，不再 complete 真零  
8. **费用**：有 commission 的样本 `fee_unknown=False`；无则 True  
9. **日内风险**：-4% 已实现 → REDUCE_ONLY，禁止新开  

---

## D. 可重复验收

```bash
cd /Users/zengyun/Downloads/我的AI/crypto
python3 scripts/round3_verify.py
# 或
python3 -m unittest tests.test_round3_acceptance tests.test_round2_acceptance -v
python3 -m unittest discover -s tests -q
```

不加载密钥、不连接账户下单。追踪表：`docs/round3_acceptance.md`。

---

## E. 尚未验证（真实外部依赖）

- 测试网 E2E 下单/撤单/Algo 实测 — **待授权**
- 策略前向有效性 / 真实盈利 — **需样本**
- 真实 MVRV / 交易所储备数据源 — **无源则权重保持 0**
- 独立工程审查官复查 — **需另约**

不得将部分成交、并发对账、幂等、配置钉死、FreeDerivatives 质量列为“以后再做”（本轮已修）。

---

## F. 最终自审

**问：我是否只是让某些测试样例通过，还是已经使真实生产链路在部分成交、并发、断线、重启、重复事件和数据失效时仍然保持一致？**

答：生产路径上的关键闭环已按反例修好并经 `on_snapshot` / `guardian.tick` / `partial_close` / `force_close` / `FreeDerivatives.get_snapshot` / `score_once` 配置钉死验证。  
已知确定性反例在离线权限内不再复现。  
**仍未证明**：真实撮合延迟、官方 Algo 账户行为、跨进程崩溃的全部 10 个崩溃点完备性、以及独立审查能否再挖出同等级新洞。  
因此结论严格限制为：**离线工程正确性通过；测试网与策略有效性未验证。**
