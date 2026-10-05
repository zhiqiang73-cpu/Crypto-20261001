# BTCUSDT 量化交易系统 · 里程碑与进度清单

> 本文档是项目的**唯一进度基线**。每个里程碑都有明确的完成标准（DoD）。状态只能在有可复现证据时才能推进，不允许口头宣称完成。

**最后更新**：2026-10-01
**维护方式**：每次提交代码后同步更新状态列，并追加到文末的变更记录。

---

## 一、项目规模基线

| 指标 | 数值 |
| --- | --- |
| Python 文件 | 114 个 |
| Python 代码行 | 25,683 行 |
| 测试文件 | 28 个 |
| 测试用例 | 306 个，**全部通过** |
| 数据采集器 | 14 个 |
| 前端代码 | 34,568 字节（`index.html` + `app.js` + `styles.css`） |
| Git 提交数 | 7 |
| 分支 | `add-getting-started`（当前）、`main` |
| 默认运行模式 | `paper`，主网锁定 |
| 备份仓库 | `https://github.com/zhiqiang73-cpu/Crypto-20261001`（提交 `53d16d6`） |

**基线验证命令**（改动后必须重跑）：

```bash
cd /Users/zengyun/我的AI/crypto
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test*.py'
```

最近一次结果：`Ran 306 tests in 0.164s — OK`

---

## 二、进度总览

| 里程碑 | 名称 | 状态 | 完成度 | 阻塞项 |
| --- | --- | --- | --- | --- |
| M0 | 项目基线与安全边界 | ✅ 完成 | 100% | 无 |
| M1 | 数据采集层 | ✅ 完成 | 100% | 无 |
| M2 | 指标与评分引擎 | ✅ 完成 | 100% | 无（改代码后需重新封存） |
| M3 | 策略接入框架 | ⚠️ 部分完成 | 70% | 缺端到端策略落地验证 |
| M4 | 交易执行与仓位管理 | ⚠️ 部分完成 | 80% | 修复后需 Testnet 重测 |
| M5 | 风控与保护单 | ⚠️ 部分完成 | 85% | Testnet 真实保护单未验证 |
| M6 | 对账与状态一致性 | ✅ 完成 | 90% | 孤立仓归属仍需人工决策 |
| M7 | 交易账本与盈亏统计 | ⚠️ 部分完成 | 65% | 缺逐笔盈亏前端展示 |
| M8 | 复盘与自我递归改进闭环 | ✅ 完成 | 85% | 待真实样本积累后验证自动采纳 |
| M9 | 前端控制台 | ⚠️ 部分完成 | 80% | 订单状态机未可视化 |
| M10 | 本地部署与 24 小时运行 | ⚠️ 部分完成 | 55% | 无守护进程与崩溃自恢复 |
| M11 | Testnet 真实验证 | ⚠️ 部分完成 | 40% | 只验证过一次开平仓 |
| M12 | 实盘前置条件 | ⛔ 未开始 | 0% | 依赖 M4–M8 全部完成 |

**整体完成度（13 项平均）**：约 **73%**

> 说明：M12 是最终目标，但它的完成条件完全依赖 M4–M8。当前项目处于"框架已成形、交易正确性未收敛"的阶段。

---

## 三、里程碑详情

### M0 · 项目基线与安全边界 ✅ 100%

**目标**：建立可运行的本地框架，并确保任何情况下都不会误连主网。

**交付物**：`trading/runtime_mode.py`、`.env.example`、`.gitignore`、`docs/standalone_local_deployment.md`、`scripts/run_local_console.sh`

**完成标准（DoD）**：

- [x] 默认 `TRADING_MODE=paper`
- [x] 主网 URL 即使被误填也不会启用
- [x] `runtime/secrets.json` 等敏感文件进入 `.gitignore`
- [x] 历史误提交的 API Key 已从 `requirements.txt` 清除
- [x] 全量测试通过（306 OK）

**证据**：`.gitignore` 已忽略 `runtime/secrets.json`、`runtime/review/`、`.env`、`.manus/`、`.tmp-router/`。

---

### M1 · 数据采集层 ✅ 100%

**目标**：稳定获取 BTCUSDT 行情与多源情绪数据。

**交付物**：`collectors/` 下 14 个采集器，其中 `binance_ws.py` 提供 WebSocket 实时 mark price 与订单簿。

**完成标准（DoD）**：

- [x] Binance Futures WebSocket 连接稳定，含自动重连与订单簿重同步
- [x] 各采集器有对应单测（`test_binance_parser.py`、`test_coinglass_parser.py` 等）
- [x] 单一采集器失败不阻断整体（`free_news` 的 stooq DXY 失败仅记录日志）

**已知非阻塞问题**：`collectors/free_news.py` 的 stooq DXY 源持续失败，仅打印日志，不影响主链路。

---

### M2 · 指标与评分引擎 ⚠️ 85%

**目标**：把多源数据汇总为可执行的交易决策（CS 综合分 → 决策档位）。

**交付物**：`indicators/`、`utils/scoring.py`、`engine/scorer.py`、`config/weights_versions/`

**完成标准（DoD）**：

- [x] 决策档位：`NEUTRAL` / `STANDARD_LONG` / `STRONG_LONG` / `STANDARD_SHORT` / `STRONG_SHORT`
- [x] 权重版本化管理（`v1.json`、`v2.json`、`ACTIVE.json`）
- [x] 评分失败连续 3 次触发 `DEGRADED` 降级
- [x] **实现指纹漂移已修复**（2026-10-01：`scripts/reseal_strategy.py` 重新封存，ACTIVE = `sb_reseal_1790851651`，复核 `load_ok: True`）

**已解决**：漂移根因是 `config/strategy_bundle.py` 的 `_IMPL_FILES`（16 个源文件）
内容指纹与已封存策略包记录的值不一致 —— 代码演进后必然发生。

修复方式**不是**关闭校验，而是新增可审计的重新封存机制：

```bash
python3 -m scripts.reseal_strategy --reason "变更原因" --dry-run   # 先看差异
python3 -m scripts.reseal_strategy --reason "变更原因"             # 正式封存
```

`config/strategy_store.py::reseal_active_strategy()` 的行为：参数继承当前 ACTIVE、
`implementation_id` 取当前代码指纹、生成全新版本号、**绝不覆盖历史版本文件**，
并记录 `parent_version` / `change_reason` / 迁移说明。

> **重要运维约束**：修改 `_IMPL_FILES` 中任一文件（含 `trading/position_manager.py`、
> `trading/executor.py`、`runtime/live_loop.py` 等）都会再次触发漂移。
> 改完代码后必须重新执行一次 reseal，否则交易路径会被正确阻断。

---

### M3 · 策略接入框架 ⚠️ 70%

**目标**：让 Claude 验证过的策略结论以**声明式数据**进入系统，而不是执行任意代码。

**交付物**：`config/strategy_contract.py`、`config/strategy_bundle.py`、`config/strategy_registry.py`、`config/strategy_store.py`、`config/strategy_versions/`、`tests/test_strategy_framework.py`

**完成标准（DoD）**：

- [x] 策略只描述条件、方向、退出规则，不执行任意 Python/JS
- [x] 策略版本化与快照封存（`sb_m1_current_baseline.json`、`sb_m2_current_baseline.json`）
- [x] 前端提供"导入 Claude 策略 JSON / 结论 / 规则"入口
- [x] 有专项单测（`test_strategy_framework.py`、`test_strategy_governance.py`）
- [ ] 端到端验证：导入一个真实策略 → 生效 → 产生信号 → 触发下单
- [ ] 均值回归与趋势追踪两个内置策略模板
- [ ] 多策略并行时的资金分配规则

---

### M4 · 交易执行与仓位管理 🔴 60%

**目标**：把决策可靠地转化为交易所订单，并维护准确的仓位账本。

**交付物**：`trading/executor.py`、`trading/position_manager.py`、`trading/binance_client.py`、`trading/models.py`

**完成标准（DoD）**：

- [x] 短期/长期双账本，组合层只下净额订单
- [x] 意图不会直接变成确认仓
- [x] 交易路径串行（`_trade_lock`）
- [x] 仓位持久化到 `runtime/review/positions.json`
- [x] 部分成交时不归零本地账本（已有测试覆盖）
- [x] **限价单成交状态机修复**（`place_limit_order` 提交后轮询确认成交，不再把 `NEW` 误判为失败）
- [ ] 开仓失败时的完整回滚

**已修复**：根因是面板在提交限价单后**立即**检查状态，把刚提交的 `NEW`
误判为失败并撤单，导致实际已成交的订单被当作未成交处理。

现在限价单统一走 `trading/binance_client.py::place_limit_order()`：

- 提交后按 `poll_interval_sec` 轮询 `query_order` 直到终态或超时
- `NEW` / `PENDING_NEW` 视为**正常中间态**，不是失败
- 网络异常时**先查询**订单再决定，绝不假设失败
- 只有交易所明确返回 `-2013 Order does not exist` 才标记 `REJECTED`
- 部分成交后即使被取消，也按实际成交量返回（供保护单按真实数量下单）
- 只有显式 `cancel_if_unfilled=True` 才撤单

覆盖测试：`tests/test_reconcile_and_limit_order.py::TestLimitOrderStateMachine`
（10 项，含成交/部分成交/超时未成交/网络异常后实际成交/显式撤单/价格与数量精度）。

**仍待完成**：修复后需在 Testnet 重新跑一次真实限价入场验证。

---

### M5 · 风控与保护单 ⚠️ 70%

**目标**：每笔仓位都有可靠的止盈止损保护，保护失败则禁止继续交易。

**交付物**：`trading/risk_guardian.py`、`trading/pretrade.py`、`trading/exit_checker.py`

**完成标准（DoD）**：

- [x] 硬止损、追踪止损、保护单创建逻辑
- [x] 日内亏损 3% 触发 `REDUCE_ONLY` 降级
- [x] 保护单失败时标记 `protection_ok=False`
- [x] Paper 模式保护单编排测试通过
- [ ] **Testnet 真实保护单端到端验证**（`STOP_MARKET` + `TAKE_PROFIT_MARKET` + `reduceOnly`）
- [x] tick size / lot size 精度校验（保护单触发价按 `PRICE_FILTER.tickSize` 对齐）
- [ ] 部分成交后的保护单数量对齐

**已修复**：`place_stop_market()` 原先把触发价硬编码为 `round(stop_price, 2)`，
这是 `-4014 Price not increased by tick size` 的直接来源 —— 在 tick 不是 0.01 的
合约上会拒绝保护单，**导致止损静默失效**。

现在改为读取 `PRICE_FILTER.tickSize` 并对齐；同时把网络错误与明确拒单分开：
只有 `-2015/-1111/-1102/-4014/-2021` 才标记 `REJECTED`，网络类保持 `UNKNOWN`
交由调用方查询确认，避免把"保护单状态未知"伪装成"保护单被拒绝"。

覆盖测试：`TestStopPriceTickAlignment`（4 项）。

---

### M6 · 对账与状态一致性 🔴 45%

**目标**：交易所真实仓位与本地策略账本永远可对账，且提供人工恢复入口。

**交付物**：`trading/position_manager.py` 的 `reconcile_on_startup()`、`flatten_orphan_exchange()`；`trading/risk_guardian.py` 的 `apply_external_position_update()`

**完成标准（DoD）**：

- [x] 启动时对账（`reconcile_on_startup`）
- [x] 孤立仓平仓路径（`flatten_orphan_exchange`，走 manager）
- [x] 外部成交同步（`apply_external_position_update`）
- [x] 不一致时阻断新开仓
- [x] **新增 `reconcile_now()` 与 `POST /api/trading/reconcile`**
- [x] Testnet 测试接口结束后强制对账（不再留下陈旧阻塞）
- [x] 密钥重绑后自动触发对账

**阻塞项（P0）**：`review/panel_server.py` 第 475 行与第 524 行的 Testnet 接口直接使用：

```python
client = review.executor.client
```

并直接调用 `client._request("POST", "/fapi/v1/order", ...)` 与 `client.market_open(..., reduce_only=True)`，**完全绕过 `review.executor.manager`**。结果是交易所仓位变了、本地状态没变，guardian 永久拒绝开仓，只能靠重启恢复。

第二个缺口：第 659 行 `review.executor.manager.client = review.executor.client` 重绑客户端后**未触发对账**。

第三个缺口：**系统内没有任何"立即对账" API**，`reconcile_on_startup()` 只在启动时执行一次。

---

### M7 · 交易账本与盈亏统计 ⚠️ 65%

**目标**：每笔交易的入场、退出、手续费、已实现盈亏和退出原因都可追溯。

**交付物**：`review/trade_ledger.py`、`review/settle.py`、`review/stats.py`、`review/journal.py`、`trading/fee_adapter.py`

**完成标准（DoD）**：

- [x] 交易账本与结算逻辑
- [x] 手续费适配
- [x] 每日统计与日记
- [x] 日终结算调度（`runtime/scheduler.py`）
- [ ] 逐笔盈亏在前端可视化
- [ ] 胜率、盈亏比、最大回撤等策略绩效指标
- [ ] 按策略维度的归因统计

---

### M8 · 复盘与自我递归改进闭环 ✅ 85%

**目标**：系统能根据交易结果自动复盘、提出策略改进建议，形成自我递归改进闭环。

**交付物**：`review/statistical_review.py`（统计引擎）、`review/meta_review.py`（收敛控制）、
`review/review_loop.py`（触发 / 护栏 / 采纳）、`review/overrides.py`（版本留档与回滚）、
`review/stats.py`（统计口径）、`config/strategy_registry.py`

**架构决定（2026-10-01）：彻底移除对 LLM 的依赖。**

原先「读交易档案 → 提出调参建议」这一步交给外部 LLM（DeepSeek）。用户明确要求系统内
不引入任何 LLM API Key，因此改为**确定性统计引擎**。已删除：`review/deepseek.py`、
`review/prompt.py`、`review/prompts/`；`config/review.py` 的 `DEEPSEEK_*` 常量与
`config/secrets.py` 的 deepseek 白名单字段一并移除。

**完成标准（DoD）**：

- [x] 统计复盘引擎：四条规则（权重判别力 / 决策阈值 / 安全阀 / 技术乘数）
- [x] 元评审收敛控制：学习率衰减、振荡锁、性能门、自动回滚、观察模式
- [x] 建议产出与采纳 / 驳回接口（`/api/proposals`）
- [x] 建议护栏：白名单、区间夹取、相对幅度、权重组归一、阈值不交叉
- [x] **零外部依赖**：不需要任何 API Key，不发起任何网络请求
- [x] 端到端跑通：交易 → 归因 → 提案 → 人工确认 → 生效
- [x] 确定性可复现：同输入必同输出（`tests/test_statistical_review.py`，22 项）
- [ ] 真实样本积累到 `VALID_SAMPLE_TARGET` 后验证首次自动采纳
- [ ] 防止过拟合的样本外验证

**统计引擎的四条规则**：

| 规则 | 信号 | 动作 |
| --- | --- | --- |
| 维度权重 | 各面「赢单均值 − 错单均值」的**组内相对**判别力 | 高于组内均值则加权、低于则减权 |
| 决策阈值 | 各档位实际胜率 | 胜率 < 45% 收紧、> 60% 放宽 |
| 安全阀 | 整体胜率 | 偏低则抬高开仓门槛 |
| 技术乘数 | 技术面判别力符号 | 反向判别时抑制 ADX / 布林收口乘数 |

**两条防自欺设计**：

1. **相对而非绝对** — 所有面读数普遍偏高只反映市场状态，不是调参信号。
   因此只用「组内相对判别力」；整体同向抬高不产生任何改动（有专门测试钉死）。
2. **对称失效单独处理** — 带符号均值会正负相消。错单里读数幅度明显更大的面，
   即便判别力为正也会被打折，理由写进提案供人工复核。

**实测输出（合成 110 笔有效样本，全程无 Key 无网络）**：

```text
建议 P1790853368373-5f5559  [pending]  引擎 statistical
诊断: short_term 权重（有效 94 笔，胜率 47.9%）：判别力 预测面 +12.00 > 数据面 +10.80
      > 消息面 +6.79 > 技术面 +3.87；相对均值加权 预测面、减权 技术面。
      安全阀：整体胜率 45.5% 偏低，50 → 53。
护栏说明: 候选 8 条超过护栏上限 5 条，按信号强度保留前 5 条；
          权重组 short_term 触发归一, 和 1.0134 → 1.0
```

---

### M9 · 前端控制台 ⚠️ 75%

**目标**：提供独立的本地交易控制台，覆盖账户、策略、仓位、日志、风控。

**交付物**：`frontend/index.html`（11,544 B）、`frontend/app.js`（8,920 B）、`frontend/styles.css`（14,104 B）、`docs/new_frontend_design.md`

**完成标准（DoD）**：

- [x] 六个页面：总览 / 策略中心 / 仓位与订单 / 交易日志 / 风控闸门 / 账户连接
- [x] 接 Binance 实时 WebSocket 行情（后端 `/api/market` 转发）
- [x] 账户余额与交易所仓位实时显示
- [x] Testnet 密钥本地表单提交（不经过聊天）
- [x] Paper 下单与自动平仓测试入口
- [x] **深色交易终端视觉**（`color-scheme: dark`，已完成但**尚未提交 Git**）
- [x] 对账状态告警可视化（账户页显示「对账状态」+「立即对账」按钮）
- [ ] 订单状态机实时展示（NEW / PARTIALLY_FILLED / FILLED / CANCELED）
- [ ] 逐笔盈亏与策略绩效面板
- [ ] 移动端适配打磨

**明确不做**：K 线图（用户明确不需要）。

---

### M10 · 本地部署与 24 小时运行 ⚠️ 55%

**目标**：在用户自己的电脑上长期稳定运行，无人值守。

**交付物**：`scripts/run_local_console.sh`、`scripts/start_new_frontend.sh`、`docs/standalone_local_deployment.md`、`runtime/scheduler.py`、`runtime/live_loop.py`

**完成标准（DoD）**：

- [x] 一键启动前后端（8787 + 8788）
- [x] 默认 `paper` 模式
- [x] 长驻评分循环（`live_loop`）
- [x] 日终任务调度
- [x] 退出时 trap 清理子进程
- [ ] 进程守护与崩溃自恢复（launchd / systemd）
- [ ] 日志轮转与磁盘占用控制
- [ ] 断网重连与数据缺口补齐
- [ ] 长时间运行稳定性验证（≥ 48 小时）
- [ ] 开机自启

**已知环境限制**：绑定 `127.0.0.1` 端口在部分沙箱环境下会被本地执行策略拒绝，需要单独申请权限。本机 Python 为 3.9（Xcode 自带），依赖装在 `~/Library/Python/3.9`。

---

### M11 · Testnet 真实验证 ⚠️ 40%

**目标**：在 Binance Futures Testnet 上验证完整交易链路真实可用。

**已完成**：

- [x] Testnet 账户连接与密钥验证（`configured: true`、`connected: true`）
- [x] 余额读取（约 4,952 USDT）
- [x] 一次限价入场 + `reduceOnly MARKET` 平仓（`0.001 BTC`）
- [x] 平仓后核验 `LONG 0.001 BTC → FLAT 0 BTC`

**待完成**：

- [ ] 限价单状态机修复后重测（避免误判）
- [ ] 止盈单真实触发验证
- [ ] 止损单真实触发验证
- [ ] 保护单失败时的阻断行为验证
- [ ] 部分成交场景验证
- [ ] 对账恢复流程验证
- [ ] 连续多笔交易的账本一致性验证

**重要提醒**：验证过程中产生的遗留仓位已通过 `reduceOnly` 平掉，但**不要假设当前状态至今未变**，接手后第一步应调用 `/api/trading/status` 重新读取。

---

### M12 · 实盘前置条件 ⛔ 0%

**目标**：达到可以接入真实资金的工程标准。

**完成标准（DoD）**（全部必须满足）：

- [ ] M4 交易执行正确性收敛，限价单状态机无竞态
- [ ] M6 对账机制完整，含人工恢复入口
- [ ] M5 保护单在 Testnet 真实验证通过
- [ ] M8 AI 复盘闭环跑通
- [ ] M10 连续 48 小时稳定运行无异常
- [ ] M11 Testnet 至少完成 20 笔完整交易且账本零误差
- [ ] 独立的资金管理与最大回撤硬限制
- [ ] 主网接入的二次确认与熔断机制
- [ ] 用户明确的书面授权

> 项目文档一贯立场（沿用 round2/3/4 报告的措辞）：**不宣称测试网已验证，不宣称策略可盈利，不宣称实盘可用。** 只有具备可复现证据时才允许推进状态。

---

## 四、当前迭代待办（按优先级）

### P0 · 必须优先修复

### P0 · 必须优先修复

> **状态：6 项全部完成（2026-10-01）**，327 个测试通过。

| 编号 | 任务 | 涉及文件 | 状态 |
| --- | --- | --- | --- |
| P0-1 | 新增 `PositionManager.reconcile_now()` | `trading/position_manager.py` | ✅ 完成（4 条判定规则 + 快照方法） |
| P0-2 | 新增 `POST /api/trading/reconcile` | `review/panel_server.py` | ✅ 完成（端到端冒烟通过） |
| P0-3 | Testnet 接口结束后强制对账 | `review/panel_server.py` | ✅ 完成（成功/未成交/异常三条路径都覆盖） |
| P0-4 | 密钥重绑后触发对账 | `review/panel_server.py` | ✅ 完成（`reason="keys_rebound"`） |
| P0-5 | 限价单状态机轮询 | `trading/binance_client.py::place_limit_order` | ✅ 完成（提交后轮询确认） |
| P0-6 | 限价单与对账单测 | `tests/test_reconcile_and_limit_order.py` | ✅ 完成（21 项） |

**对账判定规则**（`reconcile_now`）：

| 场景 | 判定 | 结果 |
| --- | --- | --- |
| 交易所与本地一致 | `consistent` | 清除阻塞，恢复开仓 |
| 本地空、交易所有仓 | `orphan_exchange_position` | 保持阻塞，记录孤立仓数量 |
| 本地有仓、交易所已空 | `external_flat_applied` | 清空本地账本，解除阻塞 |
| 两边都有仓但数量不符 | `quantity_mismatch` | 保持阻塞，不伪造归属 |
| 交易所查询失败 | `get_position` | 保持阻塞，不假装成功 |

### P1 · 紧随其后

| 编号 | 任务 | 涉及文件 | 状态 |
| --- | --- | --- | --- |
| P1-1 | 修复实现指纹漂移 | `config/strategy_store.py`、`scripts/reseal_strategy.py` | ✅ 完成（可审计 reseal） |
| P1-2 | 交易接口统一结构化错误返回 | `review/panel_server.py` | ✅ 完成（Testnet 接口带 `stage` 字段） |
| P1-3 | 保护单 tick/lot 精度 | `trading/binance_client.py` | ✅ 完成（触发价按 tick 对齐） |
| P1-4 | 复盘闭环去 LLM 化 | `review/statistical_review.py`、`review/review_loop.py` | ✅ 完成（统计引擎替代） |
| P1-5 | 前端对账告警可视化 | `frontend/` | ✅ 完成 |
| P1-6 | 订单状态机实时可视化 | `frontend/` | ⬜ 待做 |

### P2 · 中期目标

| 编号 | 任务 |
| --- | --- |
| P2-1 | 均值回归与趋势追踪策略模板 |
| P2-2 | 策略绩效指标（胜率、盈亏比、最大回撤） |
| P2-3 | 进程守护与崩溃自恢复 |
| P2-4 | 48 小时稳定性验证 |
| P2-5 | 深色前端样式提交并同步 GitHub 备份 |

---

## 五、验证与证据规则

为避免"宣称完成但实际未验证"，本项目遵循以下规则：

1. **状态推进必须有证据。** 每个里程碑的 DoD 条目只有在有可复现证据（测试输出、日志、截图、API 响应）时才能勾选。
2. **区分离线验证与真实验证。** 假交易所单测通过 ≠ Testnet 通过 ≠ 实盘可用，三者在文档中必须分开表述。
3. **禁止合并措辞。** 沿用 round2/3/4 报告的做法，工程正确性、测试网验证、策略有效性、实盘可用性四项分别独立陈述。
4. **每次改动后重跑全量测试**，并把结果记入变更记录。
5. **密钥永不进入 Git。** 提交前必须执行 `git diff --cached --name-status` 确认清单只含 `crypto/` 路径。

---

## 六、变更记录

| 日期 | 变更 | 测试结果 |
| --- | --- | --- |
| 2026-09-15 | Round-2 离线工程验收通过 | 263 OK |
| 2026-09-15 | Round-3 离线工程正确性通过 | — |
| 2026-10-01 | Round-4 修复交付；Testnet 未验证、策略有效性证据不足 | — |
| 2026-10-01 | 新增声明式策略接入层与主网防误连闸门 | 306 OK |
| 2026-10-01 | 前端重做（BTC Quant Console 六页面） | 306 OK |
| 2026-10-01 | Testnet 首次真实限价开仓 + reduceOnly 平仓（0.001 BTC） | 306 OK |
| 2026-10-01 | 深色交易终端视觉改造 | 306 OK |
| 2026-10-01 | 创建 GitHub 备份仓库 `Crypto-20261001`（提交 `53d16d6`） | — |
| 2026-10-01 | 建立本里程碑文档与进度基线 | 306 OK |
| 2026-10-01 | **P0-5 限价单成交状态机修复**：新增 `place_limit_order` 轮询确认 | 327 OK |
| 2026-10-01 | **P0-1/2/3/4 对账恢复**：`reconcile_now()` + `POST /api/trading/reconcile` | 327 OK |
| 2026-10-01 | **P1-1 实现指纹漂移修复**：新增可审计 reseal 机制并重新封存 | 327 OK |
| 2026-10-01 | **P1-3 保护单精度修复**：触发价按 tick 对齐，网络错误不再误判拒单 | 327 OK |
| 2026-10-01 | 前端新增对账状态显示与「立即对账」按钮 | 327 OK |
| 2026-10-01 | 端到端冒烟：`POST /api/trading/reconcile` → HTTP 200 `stage=consistent` | 327 OK |
| 2026-10-01 | **移除全部 LLM 依赖**：新增统计复盘引擎，删除 deepseek/prompt/prompts | 349 OK |
| 2026-10-01 | 调度器与面板改为纯算法复盘，`/api/key` 等 LLM 密钥入口已删除 | 349 OK |
| 2026-10-01 | 端到端验证：模拟 110 笔有效样本 → 产出建议，无 Key 无网络 | 349 OK |

---

## 变更记录 · 2026-10-02

### 本次交付（按用户 2026-10-02 指令）

**1. 下单方式：市价单 → 限价单 + 追价循环**

用户要求：*"把市价单委托的方式改为限价单，并且要求能够成交"*，并选定
*"B. 挂单 + 追价循环"*，目标是 **吃 maker 手续费同时保证成交**。

- `trading/binance_client.py` 新增 `place_limit_chase()`
  - 价格阶梯（做多）：`mark−off → mark−off/2 → mark → mark+off/2 → mark+off → mark+2off`
  - 档 0 为被动挂单（maker）；最后一档穿越盘口确保成交
  - **撤单/成交竞态处理**：每档撤单后必须复核订单状态，`-2011` 按已成交处理
  - 追完所有档仍未成交 → **明确返回 `chase_exhausted` 失败**，绝不静默挂单
  - 参数：初始偏移 2bps、3 秒轮询、5 秒追价、最多 6 档、整笔 120 秒超时
- `shadow/deploy.py` 入场与反手平仓全部改走 `place_limit_chase`
  - 日志新增 maker/taker、追价档数、实际成交价
- `review/panel_server.py` 冒烟与平仓按钮改走追价
- 顺带修复：冒烟接口中 `entry.filled_qty` 的属性错误（`OrderResult` 无此字段）

**2. 前端：6 页 → 2 页**

- **第 1 页 · 交易总览**：余额、当前仓位、当前委托、**当前策略**、
  赚了多少（笔数/胜负/胜率/手续费）、**历史委托**、**历史成交**
- **第 2 页 · 账户连接**：API Key 管理、连接状态、冒烟/平仓/对账按钮
- 删除：策略中心页、"粘贴 Claude 结论"导入框、交易日志页、风控闸门页
- 所有交易数据取自币安接口，不由本地账本推算

**3. 新增后端能力**

| 方法/路由 | 说明 |
| --- | --- |
| `client.all_orders()` → `GET /api/binance/orders` | 币安历史委托 |
| `client.user_trades()` → `GET /api/binance/trades` | 币安历史成交（含 realizedPnl / commission） |
| `GET /api/account/summary` | 余额、已实现/未实现、手续费、胜率、盈亏比 |
| `GET /api/strategies/active` | 运行中策略定义 + 运行态（数组，为多策略预留） |

**4. 策略单一事实来源**

新增 `config/strategies/deployed_kdj_extreme_v1.json`，描述**实际运行**的策略：
KDJ 极值反转，金叉且 K<30 做多 / 死叉且 K>70 做空，反手出场。

> 说明：注册表中另有 `trend_filter_long_v1`（`enabled: false`），是早期未启用的策略，
> UI 中以灰色"未启用"明确区分，不与运行中策略混淆。

**5. 清理废弃策略（2026-10-02）**

`kdj_rsi_reversal_v1`（KDJ+RSI 双金叉反手）经用户确认已无用，从注册表删除。
该策略从未启用（`enabled: false`），无代码依赖，删除不影响运行链路。

### 验证结果

- 新增 `tests/test_limit_chase.py`（15 项）
- **全量回归 364 项全部通过**（原 349 + 新增 15）
- `node --check frontend/app.js` 通过
- 接口实测（Testnet 真实数据）：
  - `/api/strategies/active` 正确返回 3 个策略，运行中的标记 `enabled=True`
  - `/api/account/summary`：余额 4951.29、已实现 −23.83、手续费 7.18、净 −31.01、9 笔成交、胜率 25%
  - `/api/binance/orders` 返回 10 条真实委托
  - `/api/binance/trades` 返回 9 条真实成交

### 仍待解决

- **24 小时不间断运行未实现**：会话服务存活上限约 2 小时，需 launchd 守护
- 灾难止损与风控熔断尚未在真实行情中触发过
- 追价的实际 maker 占比需累积样本后统计

---

## 2026-10-02（第二次）· 统计起算日 + 策略清理

### 1. 删除 `trend_filter_long_v1`
用户确认「趋势过滤多头 (BTCUSDT 4h)」也已无用，从 `config/strategies/` 删除。
至此注册表只剩**运行中的唯一策略** `deployed_kdj_extreme_v1.json`；
上文提到的灰色「未启用」策略说明随之作废。

### 2. 盈亏与胜率统计起算日 = 2026-10-02
用户要求「赚了多少」「胜率之类的」「历史委托」「历史成交」全部从 10-02 起算。

- `review/panel_server.py` 新增 `STATS_START_DATE`（默认 `2026-10-02`，
  可用环境变量覆盖）与 `stats_start_ms()`，按本机时区当日 00:00 换算为毫秒。
- `trading/binance_client.py` 的 `all_orders()` / `user_trades()` 新增
  `start_time` 参数，直接透传币安 `startTime`，由交易所侧过滤。
- `/api/account/summary`、`/api/binance/orders`、`/api/binance/trades`
  三个接口全部应用该起点，并在响应中回传 `stats_start` 供前端标注。

### 3. 指标口径修正
- 新增 `closed_trades`（已平仓笔数 = 胜 + 负），与 `trade_count`（成交笔数，
  含开仓腿）区分，避免「笔数」与「胜负」数量对不上。
- `profit_factor` 在无亏损时返回 `null`（前端显示「—」），不再错误显示 0.00。
- 第一页 FEES 卡片改为 **PROFIT FACTOR**，累计手续费移到卡片下方说明行。

### 验证结果
- **全量回归 364 项全部通过**
- `node --check frontend/app.js` 通过
- 接口实测（`stats_start=2026-10-02`）：
  - `/api/account/summary`：已实现 1.739、手续费 3.187、净 −1.448、
    成交 2 笔 / 已平仓 1 笔、胜 1 负 0、盈亏比 `null`
  - `/api/binance/orders`、`/api/binance/trades` 各只返回该日起 2 条记录
    （未加过滤时分别为 10 条 / 9 条）
  - 前端 index / app.js / styles.css 全部 HTTP 200

### 备份
远端 `backup/main` 经核实已包含上一轮「删除 kdj_rsi_reversal_v1」提交
（`1d59ecf`），此前记录的推送失败实为误判。

---

## 2026-10-02（第三次）· 市场错位事故修复 ★ 严重

### 事故：行情腿走主网，下单腿走测试网

用户发现并定位：**bot 用主网的 K 线算 KDJ，却在测试网下单。**

```
shadow/deploy.py:29  from shadow.live import fetch
shadow/live.py:33    BASE = "https://fapi.binance.com/fapi/v1/klines"   ← 主网
trading/binance_client.py:3  下单默认 https://testnet.binancefuture.com ← 测试网
```

两条腿踩在两个市场上，价格序列不同。用户用两条序列分别重算 bot 记录的 K/D：
12 根全部对主网命中（误差 0.00），对测试网平均差 29.73 点、最大 276.70 点。

### 代价（已核实）

信号比测试网真实信号**晚 2 根 K 线（30 分钟）**。那一笔空单实际成交 84,776.70：

| 进场依据 | 进场价 | 毛盈亏 | 手续费 | 净 |
| --- | --- | --- | --- | --- |
| 按测试网信号（应为） | 84,843.2 | +103.5 点 | — | 正 |
| 按主网信号（实际） | 84,776.70 | +37.0 点 | 84.8 点 | **−2.24 USDT** |

晚的 30 分钟吃掉了约 1.8 倍毛利。出场同样错位：主网 06:00 那根 K 28.95 触发金叉，
测试网同根 K 36.04 根本没触发。

### 根因

不是策略问题，是**行情地址和账户地址各有各的来源**，没有任何机制保证两者一致。

### 修复

**1. 新增 `config/market_endpoints.py` —— 行情地址的唯一来源**

刻意做成**由账户地址反推**（`source=account_base_url`）：账户在哪，行情就在哪，
两者不可能再分叉。仅当账户地址缺失时才退回按 `TRADING_MODE` 推断，并在
`source` 里标注退化原因。

实测确认的地址表：

| 市场 | REST | WS |
| --- | --- | --- |
| 主网 | `https://fapi.binance.com` | `wss://fstream.binance.com` |
| 测试网 | `https://testnet.binancefuture.com` | `wss://fstream.binancefuture.com` |

> `demo-fapi.binance.com` 与 legacy 测试网返回**完全相同**的 K 线，是同一市场。
> ⚠️ `wss://stream.binancefuture.com` 实测是**另一个市场**，已列入 `FORBIDDEN_HOSTS`。

**2. 一致性硬闸门**

`assert_market_consistency()` 在下单链路启动前校验行情腿与下单腿同市场，
不一致直接**拒绝启动**。`shadow/deploy.py` 启动时打印双腿地址横幅。

**3. 所有硬编码地址改为跟随账户**

- `shadow/live.py`：K 线基准由解析器给出
- `config/mapping.py`：采集层 WS/REST 跟随账户
- `frontend/app.js`：WS 地址改为后端下发（`/api/market` 新增 `market_ws`），
  拿不到地址就只重试，**绝不退回主网**

**4. 外部情绪数据单独命名**

`/futures/data/*`（多空比、持仓量历史）**只有主网提供**，测试网返回 301。
它不是交易行情、不参与下单决策，因此新增 `SENTIMENT_REST` / `BINANCE_SENTIMENT_REST`
单独命名，恒为主网是**有意的**，与事故性质不同。

### 回归闸门（防止复发）

新增 `tests/test_market_endpoints.py`（24 项），其中两条是结构性的：

- `test_shadow_live_klines_follow_account_market` —— 实际生效的 K 线地址
  必须与账户地址同市场。**这条失败即说明事故复发。**
- `test_no_hardcoded_mainnet_market_data_in_source` —— 源码静态扫描，
  `shadow/live.py` / `config/mapping.py` / `frontend/app.js` 不得再出现主网行情字面量

### 数据清理

被主网 K 线污染的日志已**归档而非删除**，移到
`runtime/shadow/_archive_mainnet_klines_2026-10-02/`，附 README 说明为何不可用于复盘。
交易记录本身是真实发生过的测试网成交，交易所侧可查，保留。

### 验证结果

- **全量回归 388 项全部通过**（原 364 + 新增 24）
- 运行器启动横幅：`行情腿: 测试网 (testnet) K线基准=https://testnet.binancefuture.com`
  / `下单腿: https://testnet.binancefuture.com` / `地址来源: account_base_url`
- 同一根 01:45 K 线：主网算出 K=85.56 / D=79.97，**测试网算出 K=50.73 / D=49.99**，
  收盘价 84863.80 与测试网一致
- 后端 `/api/market` 返回 `market=testnet`、`market_ws=wss://fstream.binancefuture.com/stream`
- 后端采集器日志确认连的是测试网 WS，`free_deriv 202` 错误消失

---

## 2026-10-02（第四次）· 下单路径改为 post-only 被动挂单

### 用户指令

> 「必须走限价单。」（同时把 `RISK_R` 从 0.01 提到 0.03，仓位放大 3 倍）

### 挂价方向 —— 这是省手续费的全部要害

**要付 maker 手续费，订单必须躺在盘口里等（提供流动性）。**

| 方向 | 挂价 | 结果 |
| --- | --- | --- |
| 做多 | **最优买价之下** | 躺进盘口 → **maker 0.0200%** |
| 做空 | **最优卖价之上** | 躺进盘口 → **maker 0.0200%** |
| 做多 | 市价之上 | 立刻吃对手单 → taker 0.0400% |
| 做空 | 市价之下 | 立刻吃对手单 → taker 0.0400% |

> ⚠️ 方向写反不是「不省」，是**手续费翻倍**。曾出现过「做多挂高于市价、
> 做空挂低于市价」的表述 —— 那恰好是保证成交但必然吃 taker 的方向。

### 实现

被动阶段一律 **post-only**：币安期货不是订单类型而是 TIF，
`type=LIMIT` + `timeInForce=GTX`。该模式下交易所**直接拒单**任何会立即成交
的订单（`-5022 could not be executed as maker`）—— 所以「成交即 maker」
不是约定，是交易所规则。用 `LIMIT_MAKER` 会得到 `-1116 Invalid orderType`。

```
做多: px = min(best_bid − pad×tick, best_ask − tick)   # 严格低于 best_ask
做空: px = max(best_ask + pad×tick, best_bid + tick)   # 严格高于 best_bid
```

挂价基于**真实盘口**（`/fapi/v1/ticker/bookTicker`）而非 mark price ——
mark 与真实最优价差一个点差，用它算出的「被动价」可能已穿盘口。

配套行为：

- 被 post-only 拒单 → 让开一档重挂（上限 `PASSIVE_MAX_PAD=5`）
- 每 `PASSIVE_REPRICE_SEC=5s` 撤单重读盘口重挂，最多 `PASSIVE_MAX_REPRICE=60` 次
- 窗口 `PASSIVE_WINDOW_SEC=180s` 耗尽 → 兜底穿盘口限价单（taker），
  滑点上限 `PASSIVE_CROSS_TICKS=5` 个 tick
- 撤单后必须复核订单状态：撤单与成交存在竞态，`-2011` 同样按成交处理

### 实测：确实吃到了 maker

账户全部 18 笔成交按交易所返回的 `maker` 标记核对：

```
10-01 18:30:59  SELL 0.0470 @ 84776.70   1.5938   0.0400%  taker
10-01 22:15:55  BUY  0.0470 @ 84739.70   1.5931   0.0400%  taker
10-02 02:22:12  BUY  0.0010 @ 84902.00   0.0170   0.0200%  maker  ←
10-02 02:22:14  BUY  0.0007 @ 84902.00   0.0119   0.0200%  maker  ←
10-02 02:22:14  BUY  0.0003 @ 84902.00   0.0051   0.0200%  maker  ←
10-02 02:22:20  BUY  0.0020 @ 84902.00   0.0340   0.0200%  maker  ←
```

4 笔 maker 全部来自新的被动挂单路径，费率精确落在 0.0200%。
按策略仓位（0.047 BTC ≈ 3,984 USDT）算：一开一平 maker 约 1.59 USDT，
taker 约 3.19 USDT，**每轮往返省约 1.6 USDT**。

### 核对信号的正确入口

新增 `GET /api/strategy/reading` + 第 1 页「策略当前读数」，直接展示运行器
落盘的 `runtime/shadow/latest_reading.json`：市场、K 线地址、OHLC、K/D、ATR
全都在。**核对信号以此为准，不要拿交易所图表比对** —— 币安测试网 UI 的图表
显示的是主网行情，而执行引擎走测试网盘口，两者必然对不上。

### 测试

`tests/test_limit_chase.py` 原按已删除的 `CHASE_*` 常量导入，导致回归 374 项
1 项错误。重写为被动挂单语义（22 项），覆盖：挂价方向、绝不穿盘口、
post-only 拒单退档、竞态成交、兜底穿盘口、滑点上限、重挂次数上限。

**全量回归 395 项全绿。**

已知小瑕疵（未改源码）：`window_sec=0` 是 falsy，会被
`window_sec or PASSIVE_WINDOW_SEC` 吃成默认 180 秒。调用方目前不传 0，暂无影响。

### 并发编辑事故

### 修复：历史委托/成交被「统计起算日」误过滤

用户报「测试记录在历史里没有」。排查结论：**记录一直都在**，是我们自己藏掉了。

`/api/binance/orders` 与 `/api/binance/trades` 复用了 `stats_start_ms()`
（统计起算日 2026-10-02），把起算日之前的委托/成交全部过滤掉 —— 于是 09-27
的首批冒烟测试、10-01 那几笔「做多后立即平仓」在页面上凭空消失。

**起算日只该作用于统计指标，不该作用于历史列表。** 已移除这两处的过滤，
并把前端拉取上限从 50 提到 200、去掉历史表上的「自 X 起算」字样。

修复后：历史委托 19 笔、历史成交 18 笔（最早 09-27 11:41 UTC）；
「赚了多少」统计仍从 10-02 起算（11 笔）。

同时核实：`testnet.binancefuture.com` 与 `demo-fapi.binance.com`
**是同一个账户体系** —— 同一账户别名 `mYmYFzoCmYAuTi`、同样余额、同样 19 笔
委托。币安网页版走 `demo.binance.com/bapi/demotrading/`，底层就是这个账户。

本轮发现**另一个 agent 在同一工作区并行修改** `trading/binance_client.py`
与 `shadow/engine.py`（10:14–10:22），一度使回归为红。已与用户确认该改动为
其授意。**多 agent 共用一个工作区时必须先确认归属再提交**，否则会把半成品
打包进提交。

### 2026-10-02 11:02 · 策略与交易记录二次审计（本轮）

用户要求从「金叉/死叉 + K 极值」改成**仅金叉/死叉**。已将 `shadow/engine.py`、`shadow/live.py` 和 `shadow/deploy.py` 的执行信号统一到 `shadow/signals.py::crossing()`；K/D 数值继续保留供审计，不再参与过滤；JSON 策略卡也同步。发现并修复了执行器原先反向信号**只平仓、不重新开仓**的缺陷：现在只在本单平仓已成交、交易所仓位归零且风控允许的情况下尝试反向开仓，并再次查询交易所确认方向和数量。此路径尚未进行真实 Testnet 反手成交验收。

用户指出币安页面与本地记录不符。直接对比同一测试网密钥查询和本地 API：两者订单 ID 序列逐个相同（19/19），成交 ID 序列逐个相同（18/18）；但此前页面将币安**默认最近 7 天 / 最多 200 条**错误标作「全部历史」，而且市价单的委托价 0 被当作真实成交价、逐笔成交数被当作已平仓数。现已补充委托号、成交 ID、Maker/Taker、委托价与均价、明确时区（UTC+8）、数据窗口与截断警告；汇总查询失败时不再展示假零。与用户币安网页 UI 的逐条对比**仍未完成**：当前会话无法读取用户先前的临时截图，也没有登录态，必须取得同一测试网账户的具体委托号/成交号及网页筛选区间才能最终确认。

前端仍为两页，重做可读性和深色视觉层级；对配置启用、最近读数、实际下单进程状态作区分。新规则单测及全量回归 **402 项通过**，前端脚本语法、策略 JSON 通过；本地 API 和前端返回 200。后端 `api/trading/status.enabled=false`，测试网仓位 FLAT、无挂单；测试网策略执行器已暂停，持续观察进程的启动也未获权限审批，不得声称新策略已上线自动下单。币安账号 Web UI 和 API 是否对应同一账户仍须用户侧进一步核对。

**风险阻塞**：`shadow/deploy.py` 尚无每轮持续执行的灾难止损，累计回撤只停止新仓、未按原规格立即平已有仓；另无已验证的 24 小时守护。因此不能将系统标记为无人值守自动交易就绪。此前记录中任何「完整历史」或「读数可替代币安网页账户核对」的措辞，以本条更正为准。

### 2026-10-02 11:12 · 十点二十二分多笔成交的归因更正

用户已确认在币安模拟交易网页中找到了 10:22 记录。按 Testnet 原始订单号核实：4 笔委托中 1 笔零成交撤单、3 笔成交；3 笔已成交委托拆成 5 条逐笔成交，其中两笔买入合计 0.004 BTC，随即 reduceOnly 卖出 0.004 BTC。这正是本文件早先“实测确实吃到了 maker”中记录的费率功能测试，**不是 5 个 KDJ 信号，也不是 KDJ 策略自动反手**。10:22 时最近两根已收盘 K 线 K>D 均未发生新交叉，运行器日志只有“观察”。10:15–10:16 还有两组各 0.002 BTC 的立即买入/卖出，亦无策略信号；现存旧订单无调用者强制标签，不能进一步精确指认发起人。

这九条额外逐笔成交毛盈亏 −0.04839999、手续费 0.47593102，净 −0.52433101 USDT；与账户 4951.28531733 → 4950.76098632 的余额变化完全一致。详细订单号与时间见 `outputs/2026-10-02-audit/10-22-成交溯源.md`。前端已把历史和汇总明确标为**账户所有来源**，并为非策略测试按钮增加醒目提示及输入确认。前后端重启、接口 200，自动下单运行器仍暂停；未来要做策略独立绩效必须建立委托来源标签和交易所成交回写，绝不可将全账户逐笔成交数直接等同于 KDJ 策略下单次数。

### 2026-10-02 11:20 · 订单来源分类：把测试单与真实交易分开

用户要求「把测试订单删掉，只保留真实的订单」。**交易所侧的历史不能删除**，也不该删除——删了就是用户最反感的假数据。因此改为**可核验的分类 + 默认隐藏 + 可随时切回**，交易所记录一条不改。

**新增 `review/order_sources.py`**，按优先级判定来源：策略台账委托号 → `kdj` 前缀 → `web_` 前缀 → `smk`/`smoke` 前缀 → `usr` 前缀 → 本系统前缀且落在**已记录的测试窗口**内 → 其余为 `未判定`。关键约定：**判不出来时保持可见**，绝不把真实成交猜成测试单藏起来。

**新增策略委托号台账** `runtime/shadow/deployed_orders.jsonl`：运行器每次真实下单后追加 `order_id`/`client_order_id`/动作/成交价。这是「策略赚了多少」唯一可靠的依据。已按进度文档记录回填两笔有据可查的历史策略单（`28613288405` 开空、`28613504175` 反手平仓）。

**下单前缀**：`place_limit_chase` 新增 `tag` 参数。策略运行器用 `kdj`，面板冒烟测试用 `smk`，用户在面板主动平仓用 `usr`。此前一律是 `ps`/`cx`/`lmt`，无法区分调用方——这正是 10:22 那 5 笔成交查不清来源的根因。

**当前账户 19 笔委托 / 18 笔成交的来源构成**：策略自动 2、网页手动 6（委托）/5（成交）、功能测试 11，未判定 0。页面默认隐藏 11 笔功能测试，显示 8 笔委托、7 笔成交；取消勾选可完整查看全部 19/18 笔。委托表与成交表新增「来源」列（位于委托号之后，窄窗口也能看到）。

**测试**：新增 `tests/test_order_sources.py`（25 项），覆盖各来源判定、测试窗口边界、台账解析、未知来源保持可见、成交沿用委托来源、标注不改原对象。**全量回归 427 项全绿**。

### 2026-10-02 11:40 · 09:45 金叉核查与「停机静默跳根」修复

用户对照图表指出「最近一笔应该是 09:45 的金叉做多」。核查结论分三段：

**1. 信号数学正确。** 用测试网 15m K 线独立重算 KDJ(9,3,3)，北京 09:45（UTC 01:45）前一根 K 26.09 ≤ D 49.62、本根 K 50.73 > D 49.99 → 金叉成立；系统日志同根 K=50.73 / D=49.99，与重算逐位一致。

**2. 当时系统没做错，是规则不同。** 该根收盘于北京 10:00，当时生效的仍是「金叉 且 K<30」（K 极值条件是用户 10:47 才要求去掉的），K=50.73 不满足，故记「观察」。代码证据为当时运行的 `5823cd3`：`engine.py:293 sig_long = bool(gold and k[i] < K_LONG_MAX)`、`deploy.py:146` 同款判定。旁证：系统真正成交的两笔（18:15 开空 K=79.85>70、22:00 反手 K=28.95<30）均满足旧规则。**按新规则 09:45 是有效做多信号，但发生在规则变更之前，无法追溯执行。**

**3. 真实缺陷已修：停机期间收盘的 K 线被静默跳过。** 旧实现每轮只处理最新一根已收盘 K 线，停机期间收盘的既不处理也不留痕，事后无法判断是否漏过信号（日志停在 UTC 02:30，02:45 之后的根没有记录）。

修复：新增 `plan_pending()`（列出晚于 `last_ts` 的已收盘 K 线，除最新一根外逐根补记为「错过」，回看窗口 96 根=24 小时，超出部分明确计数）；补记行写明该根是金叉/死叉/无交叉及「未下单」；累计 `missed_bars`、`missed_signals` 并进入读数快照，第 1 页显示「停机漏过 K 线 N 根 / 其中带交叉信号 M 根」。**补记不补下单**——旧信号在现价成交就是错误成交，补记只让「漏过什么」可查。

新增 `tests/test_missed_bars.py`（9 项）。**全量回归 436 项全绿**。

**当前状态**：自动下单运行器仍暂停，测试网仓位 FLAT、无挂单，本次排查未创建任何订单。恢复交易需用户明确同意；恢复前建议先定下 `r=0.03` 的风险比例与尚未持续执行的灾难止损。

### 2026-10-02 12:01 · Testnet 自动执行链加固（未由 Agent 启动下单）

用户要求「纯交叉策略落实、不要遗漏/暂停、正常下限价单」。本轮完成代码和可持续本机部署链路；**未由 Agent 提交或启动任何测试网自动委托**。

**策略执行**：纯 KDJ 交叉（已收盘 15m 金叉做多/死叉做空）继续作为唯一信号；K 数值阈值已完全移除。普通开仓、反手和平仓：180 秒 `LIMIT+GTX` post-only 追价（5 秒重挂、post-only 拒单退档），超时后使用带 5 tick 上限的穿盘口 `LIMIT` 兜底，不发 MARKET。

**账户启动预检**：新增 `BinanceTestnetClient.get_position_settings()`（空仓时也读到真实杠杆/逐仓）；新增 `prepare_testnet_execution()`。只有执行模式启动时才会：拒绝遗留挂单；拒绝有仓时修改双向/逐仓/杠杆；空仓无挂单时设置并复读验证「单向、ISOLATED、10x」。每次信号不再重复改账户设置。只读实测：当前账户为 Testnet、单向、ISOLATED、10x、FLAT、0 挂单。

**停机与心跳**：`plan_pending()` 已补记停机窗口的 3 根 K 线（全部无交叉）；读数快照修复了 `numpy.bool_` 不能 JSON 序列化导致页面停在旧 K 线的缺陷。快照每次循环刷新，即使同一根 K 线已处理；新增 `runner_heartbeat.json`，面板 API 和前端显示真实 `running/starting/error/blocked/stopped` 及模式，不再把历史读数冒充运行中。

**灾难止损**：新增每 15 秒检查标记价；浮亏 ≥ `3×ATR_1H` 时立即 `reduceOnly` 穿盘口 **LIMIT** 平仓（`force_cross=True`，有滑点上限、非 MARKET）。普通订单仍 180 秒 maker 优先；灾难止损不等待 maker，以免止损失效。部分成交会如实记录剩余仓位，下一轮继续尝试，绝不伪称平完。

**本机持续运行**：新增 `scripts/install_testnet_services.sh`（显式 `ENABLE_TESTNET_EXECUTION=YES` 才会安装），用 macOS `launchd KeepAlive` 启动 trader/panel/frontend/monitor 四项服务，绕过会话进程约 2 小时回收；配套 `scripts/uninstall_testnet_services.sh` 与 `scripts/run_testnet_monitor_loop.sh`。安装需用户在其本机终端自行执行，脚本不含密钥。

**验证**：新增 `test_execution_preflight.py`（8）、`test_runner_snapshots.py`（3）、`test_disaster_limit_stop.py`（6），扩展限价追价灾难穿盘口测试 1 项。全量测试 **454 项通过**；`bash -n` 三个脚本、`node --check frontend/app.js` 通过；前端实测显示「运行中 · 仅观察」且无 console error。当前仅观察运行器在会话内运行，未发送委托。

### 2026-10-02 12:40 · 第二条 5m 策略并入同一测试网运行器

用户拍板：不要 MA/OBV；5m 以 KD 金叉死叉为大前提，金叉且 K<30 做多、死叉且 K>70 做空；与 15m 共用一个 BTCUSDT 单向账户。

实现：`shadow/strategy_books.py` 各记虚拟仓，交易所只下净额；5m 冷启动不追溯旧 K 线；委托前缀 `kd5` 也算策略单；面板按 `runtime_key` 挂运行态，读数接口同时返回 15m/5m。`r` 仍为 0.03，未改。全量测试 **475 项通过**。仍是测试网限价，不宣称能赚钱、不能当实盘。

### 2026-10-02 21:14 · 人工干预检测落地 + 界面按用户要求精简

**用户指令**：左边已配置策略看不清；未实现/已实现盈亏按 BTCUSDT、ETHUSDT 分开统计；删掉「最近一根信号读数」与「账户盈亏核对」两个板块。

**已完成**
1. **人工干预检测真正生效（生产验证）**：21:02:10 用户在币安网页手动平掉 BTC(-0.001) 与
   ETH(-2.366)，运行器按订单来源识别为 `web_*` 人工单，判定 `reduce` → **清零该标的策略
   账本、不补回**，等下一根信号再开仓。账本 `entry` 已清空，仓位保持 FLAT。这是本轮修复的
   实盘证据，不再是单元测试推断。
2. **左侧策略列表可读性**：侧栏 268→312px；名称 13.5→14.5px；规则原文取消
   `-webkit-line-clamp:1` 截断，改为完整换行；行距与对比度上调。
3. **分标的盈亏**：`/api/account/summary` 新增 `by_symbol` 与 `positions_by_symbol`；
   未实现与已实现（净 = 已实现 − 手续费）都按 BTC / ETH 各自一行显示，不混加。
4. **移除两个板块**：最近一根信号读数、账户盈亏核对（PNL EVENTS / 正负笔数 / 胜率 / 盈亏比）
   连同相关 JS 渲染一并删除；`loadReading` 精简为只更新运行器心跳的 `loadRunner`。
5. **两个隐藏缺陷顺手修掉**：
   - `#externalBar` 的 `hidden` 属性被 `.notice-bar{display:flex}` 覆盖，导致提示条一直显示；
     补 `.notice-bar[hidden]{display:none!important}`。
   - 面板 API 未禁缓存，浏览器可能复用上一次的仓位/盈亏响应；中间件对 `/api/` 统一加
     `Cache-Control: no-store`。
   - `renderExternal` 曾被写到 IIFE 之外（`})();` 之后），引用闭包内 `$ / coin / num / fmtTime`
     触发 ReferenceError 并被 `loadStatus` 的 try/catch 吞掉，提示条永不显示；已移回闭包内。

**验证**：518 项测试通过；`node --check frontend/app.js` 通过；浏览器实测提示条文案
「已跟随人工操作：BTC · ETH」与两张分标的卡片（BTC +124.35 / ETH −7.74）均正确渲染，
控制台无错误。

### 2026-10-02 21:50 · 15m 策略换成 KDJ×MACD 双指标闸门

**用户指令**：把 MACD 加进技术指标后有了新想法 —— KDJ 交叉负责择时，MACD 能量柱负责
方向。确认口径：**MACD 方向按红绿柱（能量柱）正负为准；做双向；背离类信号直接丢弃不
操作；周期固定 15m**。这套思路**整条替换**原来的 15m 思路，**只改 BTCUSDT 与 ETHUSDT
两条 15m 策略**，5m 两条不动。

**规则（替换前 → 替换后，仅 15m）**

| | 替换前 | 替换后 |
| --- | --- | --- |
| 做多 | 金叉 + 收盘涨破上一根最高（≥0.15×ATR_1H） | 金叉 + MACD 能量柱为正（绿柱） |
| 做空 | 死叉 + 收盘跌破上一根最低（≥0.15×ATR_1H） | 死叉 + MACD 能量柱为负（红柱） |
| 背离 | 不适用 | 金叉遇红柱 / 死叉遇绿柱 → **丢弃**：不开仓、不平仓、不反手 |

**已完成**

1. `shadow/indicators.py::macd` 新增 MACD(12,26,9)：EMA 自第一根收盘价递推
   （α=2/(n+1)），DIF=EMA12−EMA26，DEA=EMA9(DIF)，柱=DIF−DEA，与 TradingView / 币安
   同口径。闸门只取柱的正负号。
2. `shadow/signals.py::macd_gate` / `macd_side`：闸门判定与「背离」文案的唯一来源；
   柱为 0 或数值不可用同样丢弃。
3. `shadow/strategy_books.py`：`StrategySpec` 增加 `require_macd`；`SPEC_15M` 与
   `SPEC_ETH_15M` 改为 `require_macd=True` / `require_break=False`，规则原文同步。
   5m 两条保持 `require_break=False` / `require_macd=False`。
4. `shadow/deploy.py`：`process_strategy()` 与 `save_signal_reading()` 都过闸门；停机
   补记的「错过」根数只统计过闸门的信号；读数快照新增 `MACD_DIF` / `MACD_DEA` /
   `MACD_HIST` / `macd_side` / `require_macd` / `macd_note`，并新增 `series.hist`。
5. `shadow/engine.py`（回测）+ `shadow/reporting.py`：同样过闸门，逐根日志新增
   「MACD柱」列；「宽松」列保留为未经闸门的裸交叉，用于量化闸门挡掉了多少。
   `shadow/live.py`（阶段 1 影子运行器）同步。
6. 策略卡 `config/strategies/deployed_kdj_extreme_v1.json` 与
   `deployed_kdj_eth_extreme_v1.json` 改写 entry/exit/indicators/note；5m 两张卡未动。
7. 前端 `frontend/app.js::signalView` 增加闸门文案（「金叉且 MACD 绿柱」/
   「MACD 不是绿柱」等）。
8. 测试：新增 `tests/test_macd_gate_strategy.py`（20 项：EMA 口径、闸门矩阵、引擎信号
   必须与能量柱同向、规格与策略卡一致性）；改写 `tests/test_dual_strategy_books.py`
   的 15m 用例（改用 `patch(shadow.deploy.macd)` 控制柱方向）与
   `tests/test_cross_only_strategy.py` 的策略卡断言。

**验证**

- 全量测试 **539 项通过**（含新增 20 项）。
- 真实历史回放（`python3 -m shadow.run --tail 60`，BTCUSDT 15m 5,761 根）：裸交叉
  1,020 个 → 过闸门 404 个，**闸门挡掉 616 个（60.4%）**；模式 A 9 笔成交、净
  +25.92 USDT、盈亏比 2.51；模式 B 22 笔、净 −26.68 USDT。闸门确实在挡背离，不是空转。
- `price_breaks()` / `BREAK_ATR_MULT` 保留但已无规格使用；5m 行为未受影响。
- 未改：`r=0.03`、10x 逐仓、日亏 3%、回撤 10% 熔断、灾难止损 3×ATR_1H、限价追价与
  maker 优先执行、委托前缀。`_IMPL_FILES` 不含 `shadow/`，**本次无需 reseal**。

### 2026-10-02 22:13 · 追价下单「部分成交不撤单」缺陷（真实事故）

**现象**：21:46 日志出现 `[ETHUSDT 净仓] 目标 5.0700 原 10.0100 → 5.0710`，
持仓凭空多出 4.94 ETH，一分钟后被迫反向卖出纠正。

**排查**：拉交易所逐笔委托核对，21:28 之后 ETH 只有一笔外部单（22:05 用户手动平仓
`web_Atbiyv4NXd`），**加仓那 4.94 并不是人下的**：

```
21:46:30  策略 BUY 4.940 @2762.27  FILLED   ← 补差单
21:46:30  策略 BUY 5.070 @2760.67  FILLED   ← 原单剩余部分
```

**根因**（`trading/binance_client.py` 的 `place_limit_order` 收尾）：

```python
filled = float(last.cum_filled_qty or last.filled_qty or 0)
if filled > 0:
    return last.to_order_result()      # ← 部分成交直接返回，跳过撤单
```

部分成交（0.13 / 5.070）后函数**不撤单就返回**，剩余 4.94 继续以 GTC post-only
挂在盘口。运行器以为这单已结束，下一步净仓同步又下了 4.940 的补差单，两张单在同一秒
全部成交 → 持仓正好翻倍，随后被迫反向纠正，白付一次价差与手续费。

**修复**：只要不是完全成交，就必须撤掉剩余挂单；撤单回执缺成交量时用轮询值兜底；
有真实成交时保持 `ok=True`（与改动前语义一致），`order_state` 仍如实记为 CANCELED。

**验证**：新增 `tests/test_partial_fill_cancel.py`（4 项）；全量 **543 项通过**。
**注意**：人工加仓检测逻辑本身没有问题 —— 本次并非人工下单，故未触发暂停。

### 2026-10-03 06:45 · 凌晨两笔灾难止损复盘 + 项目迁出 Downloads

**现象**：10-03 凌晨出现两笔大额亏损，账户权益从峰值 5253.74 降到 4475.20。

**核对**（交易所口径）：钱包余额 4475.20，已实现 −455.26，手续费 −91.99，
净盈亏 −547.25；胜率 71.7%（43 胜 17 负），但盈亏比仅 **0.364**（ETH 0.048）。

**两笔亏损的真实来源 —— 都是 3×ATR_1H 灾难止损，不是正常反手**

| 时间（北京） | 标的 | 动作 | 数量 | 入场 | 强平价 | 亏损 |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 02:37 | ETHUSDT | 灾难止损平仓 | 4.821 | 2725.34 | 2674.71 | ≈ −244 |
| 02:43 | BTCUSDT | 灾难止损平仓 | 0.164 | 85580.00 | 83983.00 | ≈ −262 |

日志原文：

```
ETHUSDT 浮亏 50.63 ≥ 3×ATR 50.58; LIMIT 强平 成交价=2674.71; 剩余 0.0000 ETH
BTCUSDT 浮亏 1557.27 ≥ 3×ATR 1475.50; LIMIT 强平 成交价=83983.00; 剩余 0.0000 BTC
```

**为什么会亏这么多（结构性原因，不是故障）**

1. 仓位是 `qty = 权益 × 0.03 ÷ (2 × ATR_1H)`，灾难止损在 `3 × ATR_1H`，
   两者相除 ⇒ **每次灾难止损必然亏掉 3% × 3 ÷ 2 = 4.5% 权益**。两次≈9%。
2. 这两笔都是 **5m 仓位**。5m 的出场只有「反向交叉 + K 极值」和灾难止损两条路；
   **下跌途中 K 值一直很低，永远回不到 K>70**，所以反向平仓条件无法成立，
   多仓没有任何正常出口，只能一路扛到 3×ATR。凌晨逐根日志里 ETH 5m / BTC 5m
   全程都是 `观察`（K=13.75、20.25…），没有任何一次满足平仓阈值。
3. 结果就是「赢很多次小钱、两笔大亏全部吃回」的低盈亏比结构。

**衍生问题（已处理）**

- 22:39 起本机同时存在两份运行器（手动 + launchd），02:29–02:32 触发交易所限频：
  `{"code":-1003,"msg":"Way too many requests; IP banned until ..."}`；
  另有多次 `拉 K 线失败: handshake/read timed out`。
- `launchd` 服务放在 `~/Downloads` 下会被 macOS「下载文件夹」隐私保护拦截：
  `[Errno 1] Operation not permitted: deployed_trades.csv`、
  `run_testnet_monitor_loop.sh: Operation not permitted`、
  `ModuleNotFoundError: No module named 'shadow'`。四个服务全部失效，已卸载。

**熔断**：累计回撤 14.8% ≥ 10% ⇒ `halted=True`，停止开新仓，等待人工指令。

**目录迁移（用户指令）**：整仓 `/Users/zengyun/Downloads/我的AI` →
`/Users/zengyun/我的AI`，项目现位于 **`/Users/zengyun/我的AI/crypto`**。
只搬 `crypto` 会掏空仓库（200 个跟踪文件全在 `crypto/` 下），故整仓搬迁，
git 历史与 `backup` 远程保持完好。迁移后已批量更新 16 个文件里写死的旧绝对路径，
并在新路径下复跑全量测试：**543 项通过**。

**launchd 托管现状**：Manus 的执行沙箱无法注册 GUI LaunchAgent
（`Bootstrap failed: 5: Input/output error`），需由用户在本机终端执行
`ENABLE_TESTNET_EXECUTION=YES bash scripts/install_testnet_services.sh`。
安装脚本已改为幂等：加载服务前先停掉手动实例，避免重复运行器与端口抢占。
