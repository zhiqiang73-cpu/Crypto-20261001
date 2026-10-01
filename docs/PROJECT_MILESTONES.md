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
cd /Users/zengyun/Downloads/我的AI/crypto
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
| M8 | AI 复盘与自我改进闭环 | ⛔ 未开始 | 15% | **无任何 LLM Key** |
| M9 | 前端控制台 | ⚠️ 部分完成 | 80% | 订单状态机未可视化 |
| M10 | 本地部署与 24 小时运行 | ⚠️ 部分完成 | 55% | 无守护进程与崩溃自恢复 |
| M11 | Testnet 真实验证 | ⚠️ 部分完成 | 40% | 只验证过一次开平仓 |
| M12 | 实盘前置条件 | ⛔ 未开始 | 0% | 依赖 M4–M8 全部完成 |

**整体完成度（13 项平均）**：约 **68%**

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

### M8 · AI 复盘与自我改进闭环 ⛔ 15%

**目标**：系统能根据交易结果自动复盘、提出策略改进建议，形成自我递归改进闭环。

**交付物**：`review/meta_review.py`、`review/deepseek.py`、`review/prompt.py`、`review/overview.py`、`review/prompts/daily_summary.py`、`config/strategy_registry.py`

**完成标准（DoD）**：

- [x] DeepSeek 客户端（OpenAI 兼容 `/chat/completions`）
- [x] 元评审流程与提案机制（`/api/proposals`）
- [x] 提案采纳/拒绝接口
- [ ] **配置 LLM API Key**（当前完全没有）
- [ ] 复盘闭环端到端跑通：交易 → 归因 → 提案 → 人工确认 → 生效
- [ ] 提案的样本量门槛与统计显著性校验
- [ ] 防止过拟合的样本外验证

**阻塞项（当前最大空白）**：实测三处来源均无 key：

| 检查项 | 结果 |
| --- | --- |
| `runtime/secrets.json` | 只有 Binance Testnet 三项，**无 `deepseek_api_key`** |
| 本地 `.env` | 不存在 |
| 环境变量 | `DEEPSEEK_API_KEY`、`OPENROUTER_API_KEY`、`OPENAI_API_KEY` 全部 unset |

**已核实的配置事实**：代码默认走 **DeepSeek 官方 API**（`https://api.deepseek.com`，模型 `deepseek-chat`），**不是 OpenRouter**。因为客户端是 OpenAI 兼容格式，切换到 OpenRouter 只需改 `deepseek_base_url` 为 `https://openrouter.ai/api/v1`、`deepseek_model` 为 `deepseek/deepseek-chat`，**无需改代码**。

`config/secrets.py` 白名单已允许 `deepseek_api_key`、`deepseek_model`、`deepseek_base_url` 三个字段。

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
| P1-4 | 配置 LLM Key 并跑通复盘闭环 | `runtime/secrets.json`、`review/meta_review.py` | ⛔ **等待用户提供 Key** |
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
