# Claude 本地继续开发提示词：BTCUSDT 量化交易系统

你现在接手一个已经完成第一阶段搭建的本地 BTCUSDT 量化交易系统。请**不要从零重写**，也**不要只做静态页面**。你的任务是：先完整审计现有代码与运行状态，再按优先级修复真实存在的交易正确性问题。

这份文档是上一轮工作的交接说明，包含架构、文件位置、已完成的改动、已确认的 bug 根因、安全边界和验收标准。请先读完再动手。

---

## 1. 项目位置与运行环境

| 项目 | 值 |
| --- | --- |
| 项目目录 | `/Users/zengyun/我的AI/crypto` |
| 前端地址 | `http://127.0.0.1:8788/` |
| 后端地址 | `http://127.0.0.1:8787/` |
| 默认运行模式 | `paper` |
| 交易标的 | Binance USDⓈ-M Futures `BTCUSDT` |
| 运行位置 | 纯本地 / 自有电脑，不依赖云端托管 |
| 当前允许范围 | 仅 Paper 与 Testnet，**禁止主网** |

用户的核心需求是：把在 Claude 里验证过的策略结论（一段声明式规则）丢进这个框架，系统就能自动执行；支持多策略并存（均值回归、趋势追踪等）、自动仓位、限价入场、止盈、止损、自动平仓、余额、逐笔盈亏记录，并且能 24 小时运行、根据交易结果自我复盘改进。**用户明确不需要 K 线图。**

### 1.1 Git 关键注意事项（非常重要）

当前工作目录虽然是 `crypto`，但 **Git 实际根目录在上一级**：

```text
/Users/zengyun/我的AI
```

这意味着直接 `git add -A` 会把上一级的无关内容全部带进来。实测会污染提交的项包括：`../README.md` 的删除状态、`../Auto-computer-driver/`、`../Codex/`、`../claude/` 等目录。

正确做法是在 `crypto` 目录下执行 `git add -A .`（注意那个点号，它会被解析为 `crypto/` 前缀），并且在提交前用 `git diff --cached --name-status` 确认清单里只有 `crypto/...` 路径。

另外，Git 元数据目录 `/Users/zengyun/我的AI/.git` 在某些沙箱执行环境下不可写，需要单独申请写权限才能提交。

### 1.2 已有备份仓库

```text
https://github.com/zhiqiang73-cpu/Crypto-20261001
```

`main` 分支首次提交为 `53d16d6`，共 155 个文件。注意：**这次备份发生在深色前端样式改造之前**，所以它不包含最新的 `frontend/styles.css`。如果要重新同步，请重新导出、验证后再提交。

备份时的排除项（务必保持）：`runtime/secrets.json`、`runtime/review/`、`.env`、`.manus/`、`.tmp-router/`、`__pycache__/`。

### 1.3 进度基线文档（必须维护）

项目的里程碑与进度清单在：

```text
docs/PROJECT_MILESTONES.md
```

这份文档是项目的**唯一进度基线**，包含 M0–M12 共 13 个里程碑、每个里程碑的完成标准（DoD）、当前完成度、阻塞项，以及按 P0/P1/P2 分级的待办清单。

**接手后必须遵守的规则：**

1. 开始工作前先读这份文档，确认当前所处的里程碑和待办优先级。
2. 每完成一个 DoD 条目，勾选它，并在文末"变更记录"追加一行。
3. **状态推进必须有可复现证据**（测试输出、日志、API 响应、截图）。没有证据不允许勾选。
4. 保持四项陈述相互独立：工程正确性、测试网验证、策略有效性、实盘可用性。**不要用合并措辞掩盖未验证的部分。**
5. 每次代码改动后重跑全量测试，并把结果写入变更记录。

当前整体完成度约 **68%**，处于"框架已成形、交易正确性已收敛、待 Testnet 复验"阶段。

**2026-10-01 已完成**：P0 全部 6 项（对账恢复 + 限价单状态机）、P1-1/2/3/5。
测试从 306 增至 327 项，全部通过。

**下一步优先级**：Testnet 复验限价入场与保护单 → M10 进程守护与 48 小时稳定性 →
M7 逐笔盈亏前端展示。**注意：复盘闭环已不再依赖 LLM，不再需要任何 API Key。**

> **运维约束（务必记住）**：修改 `config/strategy_bundle.py::_IMPL_FILES` 中任一文件后，
> 必须执行 `python3 -m scripts.reseal_strategy --reason "原因"`，否则实现指纹漂移会
> 正确阻断交易路径。这是设计行为，不是 bug。

---

## 2. 系统架构总览

系统分为四层，理解这四层是接手的前提。

### 2.1 数据采集层 `collectors/`

负责从多个数据源拉取行情与情绪数据：`binance_ws.py`（Binance WebSocket，含 mark price 和订单簿）、`binance_klines.py`、`free_derivatives.py`、`free_news.py`、`free_onchain.py`、`coinglass.py`、`cryptopanic.py`、`deribit.py`、`fear_greed.py`、`fred.py`、`polymarket.py`、`predict_fun.py`、`defi_llama.py`。

### 2.2 指标与评分层 `indicators/` + `engine/` + `utils/`

`indicators/engine.py` 计算技术指标，`utils/scoring.py` 做多面打分，`engine/scorer.py` 汇总为综合分（CS），产出 `NEUTRAL / STANDARD_LONG / STRONG_LONG / STANDARD_SHORT / STRONG_SHORT` 等决策。

### 2.3 交易执行层 `trading/`（核心，问题集中在这里）

| 文件 | 职责 |
| --- | --- |
| `trading/binance_client.py` | Binance Futures 客户端：签名请求、余额、仓位、订单、mark price、交易所信息、tick/lot 精度 |
| `trading/executor.py` | 交易执行器：接收快照、决定开平仓、状态汇总 |
| `trading/position_manager.py` | **组合仓位管理器**：短期/长期两本策略账本、净额下单、对账、持久化 |
| `trading/risk_guardian.py` | 风控守卫：保护单（止盈止损）、硬止损、追踪止损、开仓闸门 |
| `trading/pretrade.py` | 下单前检查 |
| `trading/exit_checker.py` | 退出条件判断 |
| `trading/fee_adapter.py` | 手续费计算 |
| `trading/fake_exchange.py` | Paper 模拟交易所（单测隔离用） |
| `trading/runtime_mode.py` | Paper/Testnet 模式安全闸门，主网默认锁定 |
| `trading/models.py` | 数据模型：`HorizonPosition`、`OrderResult`、`OrderState`、`TradeAction` 等 |

**关键设计原则（不要破坏）：** 每个 horizon 维护独立的虚拟策略账本；组合层只向交易所下**净额**订单；已确认仓位只按实际成交量或外部成交事件更新，**意图绝不能直接变成确认仓**；所有交易路径串行（`_trade_lock`）；仓位持久化到 `runtime/review/positions.json`，重启时对账。

### 2.4 运行与面板层 `runtime/` + `review/` + `frontend/`

`runtime/live_loop.py` 是长驻评分循环，`runtime/panel_server.py` 负责实时行情接口，`review/panel_server.py` 是主 API 服务（约 780 行，路由集中在 730–763 行），`review/meta_review.py` 与 `review/statistical_review.py` 负责复盘（纯算法，无 LLM）。

前端是**独立重做**的 BTC Quant Console，位于 `frontend/`，包含总览、策略中心、仓位与订单、交易日志、风控闸门、账户连接六个页面。

---

## 3. 当前 API 路由清单

`review/panel_server.py` 第 730–763 行注册了全部路由，接手时请先读这一段。重点路由：

| 路由 | 用途 |
| --- | --- |
| `GET /api/health` | 服务健康检查 |
| `GET /api/market` | 由后端 Binance WebSocket 缓存的实时行情 |
| `GET /api/trading/status` | 账户余额、交易所仓位、本地仓位、对账状态、guardian 状态 |
| `POST /api/trading/keys` | 保存 Binance Testnet 密钥并验证 |
| `POST /api/trading/toggle` | 启用/停用自动交易 |
| `POST /api/trading/close` | 按 horizon 平仓 |
| `POST /api/trading/flatten_orphan` | 平掉交易所孤立仓并解除对账阻塞（**走 manager，是正确的做法**） |
| `POST /api/paper/test-order` | Paper 限价单 + 保护单编排模拟 |
| `POST /api/paper/test-close` | Paper 止盈/止损自动平仓模拟 |
| `POST /api/testnet/smoke-limit-close` | Testnet 限价入场 + 立即平仓（**存在设计缺陷，见 §5**） |
| `POST /api/testnet/flatten-now` | 直接平掉当前 Testnet 仓位（**存在设计缺陷，见 §5**） |

---

## 4. 已完成的工作

### 4.1 恢复与安全加固

从 Git HEAD 恢复了旧有量化代码，并补齐了安全边界：`trading/runtime_mode.py` 增加 Paper/Testnet 闸门，主网 URL 即使被误填也不会在默认配置下启用；`trading/binance_client.py` 接入该闸门。

同时清理了历史遗留问题：`requirements.txt` 里曾经被误提交过 API Key（`pred_sk_...`、`sk-...`），已清除为纯依赖列表。

### 4.2 策略接入层

新增声明式策略注册表，让 Claude 产出的策略以**数据**而非**代码**的形式进入系统：

- `config/strategy_contract.py` — 策略合同
- `config/strategy_bundle.py` — 策略包序列化与校验
- `config/strategy_registry.py` — 声明式策略输入与校验注册表
- `config/strategy_store.py` — 策略版本仓库
- `config/strategy_versions/` — 版本快照
- `docs/local_system_architecture.md` — 架构说明

设计原则：策略只能描述条件、方向和退出规则，**不允许执行任意 Python/JavaScript**。

### 4.3 前端重做

`frontend/index.html`、`frontend/app.js`、`frontend/styles.css` 三件套是全新写的，不复用旧 V7 面板。后端为它加了本地 CORS 白名单（`review/panel_server.py` 第 136 行附近的 `local_frontend_cors`）。

### 4.4 深色模式改造（最新，尚未备份）

`frontend/styles.css` 已整体转为深色交易终端风格，要点如下：全局 `color-scheme: dark`；深墨背景 `#090c11` 配径向渐变；卡片用 `#121821` → `#0f151e` 渐变加内阴影；行情强调色 `#f08b5b`；风控健康色 `#62d49a`；侧边栏深色渐变加左侧橙色高亮条；输入框和下拉框统一暗色；窄屏隐藏侧边栏转单列。

已在本地浏览器截图确认渲染正常。**这份改动还没有提交到 Git，也没有进入 GitHub 备份。**

---

## 5. 已确认的 Bug 根因（最重要的一节）

### 5.1 P0：对账状态无法自动恢复

**现象：** 交易所仓位已经是 `FLAT 0.0 BTC`，但 `/api/trading/status` 仍然返回 `reconciliation_needed: true` 且 `guardian.allow_new_entries: false`，导致系统永久拒绝开新仓，只能靠重启服务恢复。

**精确根因：** `review/panel_server.py` 中的 Testnet 测试接口在第 475 行和第 524 行直接取用了底层客户端：

```python
client = review.executor.client          # 第 475 行、第 524 行
```

然后直接调用 `client._request("POST", "/fapi/v1/order", ...)` 和 `client.market_open(..., reduce_only=True)`。这**完全绕过了 `review.executor.manager`（即 `PositionManager`）**。

后果是：交易所真实仓位被改变了，但 `PositionManager` 的内部状态（`reconciliation_needed`、`_allow_new_entries`、`_local_net()`）完全没有更新，于是 guardian 一直认为账本与交易所不一致。

**第二个相关缺口：** `review/panel_server.py` 第 659 行在保存新密钥时会重新绑定客户端：

```python
review.executor.manager.client = review.executor.client
```

但**没有触发任何对账**，所以换密钥后 manager 仍然带着旧的对账状态。

**第三个缺口：** 系统里**没有任何"立即对账"的 API**。清除 `reconciliation_needed` 的唯一路径是 `reconcile_on_startup()`（`trading/position_manager.py` 第 299 行），它只在 `bootstrap()` 时被调用一次。`/api/trading/flatten_orphan` 走的 `flatten_orphan_exchange()`（第 331 行）能清标志，但它有前置条件——要求本地账本必须为空，否则直接返回 `local_not_flat` 错误。

**修复方向：**

1. 新增 `PositionManager.reconcile_now()`：重新读取交易所仓位，与 `_local_net()` 比较，一致则清除 `reconciliation_needed` 并恢复 `_allow_new_entries`，不一致则保持阻塞并记录未解释差异。
2. 新增 `POST /api/trading/reconcile` 路由暴露该方法。
3. 让 `api_testnet_limit_then_close` 和 `api_testnet_flatten_now` 在完成后**必须**调用 `reconcile_now()`，或者干脆改为通过 manager 下单。
4. 密钥重新绑定后也调用一次 `reconcile_now()`。
5. 关键原则：**一致性必须从交易所实时状态重新推导，不能靠内存里的陈旧布尔值。**

### 5.2 P0：限价单成交状态机误判

**现象：** 第一次 Testnet 限价测试时，订单实际上成交了，但程序判定为"未成交"并执行了取消，随后没有平仓，导致账户留下 `LONG 0.001 BTC` 的遗留仓位。这个仓位后来才通过读取交易所真实状态、再发 `reduceOnly MARKET SELL` 手动平掉。

**相关代码：** `review/panel_server.py` 第 499–512 行附近，逻辑是提交订单后立即检查 `entry.state.value`：

```python
entry = client._raw_to_managed(raw, client_order_id=cid)
if entry.state.value not in ("FILLED", "PARTIALLY_FILLED"):
    await client.cancel_order(order_id=entry.exchange_order_id, symbol=client.symbol)
    return web.json_response({... "error": "limit_not_filled_cancelled"}, status=409)
```

问题在于：限价单刚提交时返回 `NEW` 是**正常**的，不代表失败；而且当订单状态未知时直接取消，会与交易所真实成交产生竞态。

**修复方向：** 提交订单后必须轮询查询订单状态，等待有限时间（例如 3–5 秒，带指数退避）；正确处理 `NEW` / `PARTIALLY_FILLED` / `FILLED` / `CANCELED` / `EXPIRED`；网络异常时**先查询订单**而不是假设失败；只有确认零成交才能取消；有部分成交时必须按**实际成交数量**建立保护单和平仓量。相关字段是 `filled_qty` / `cum_filled_qty`。

### 5.3 P1：配置实现指纹漂移

后台日志反复出现：

```text
ERROR engine.scorer: 生效配置无效, 交易路径应阻断: implementation_id drift: sealed=a4620e22ebec current=d0b9956ca459
```

`sealed` 是策略包里封存的实现指纹，`current` 是当前代码算出来的。两者不一致时评分路径被阻断。需要检查 `engine/scorer.py`、`config/effective_config.py`、`config/strategy_bundle.py` 和 `config/strategy_versions/ACTIVE.json`。

**不要用关闭校验的方式绕过**，应该明确重新生成合法的配置快照，或修正指纹计算流程。注意：`engine/scorer.py` 里有一段为兼容旧覆盖测试而保留的分支，修改时不要破坏 `tests/test_review_guard.py`。

### 5.4 P1：交易接口异常返回不一致

Testnet 接口曾因未捕获 `BinanceClientError` 而返回 HTTP 500，前端只看到 `Failed to fetch`，无法判断真实原因。所有交易接口都应该捕获异常并返回结构化 JSON，包含 `ok`、`stage`、`error`、`orderId`、`clientOrderId`、HTTP 状态。**绝不能在响应里带出密钥。**

### 5.5 P1：tick size 与精度

曾经出现过一次 Binance 400 错误：

```text
{"code":-4014,"msg":"Price not increased by tick size."}
```

修复方式是读取 `exchange_info()` 的 `PRICE_FILTER.tickSize` 并对价格取整。数量精度同理，用 `_lot_step()` 和 `_qty_precision()`。保护单也必须遵守同样的 tick/lot 约束。

---

## 6. 复盘引擎：已彻底移除 LLM（2026-10-01）

**结论：本项目不再需要任何 LLM API Key，也不再发起任何模型请求。**

原先「读交易档案 → 提出调参建议」这一步交给外部 LLM（DeepSeek）。用户明确要求系统内
不引入 LLM Key，因此已替换为确定性统计引擎 `review/statistical_review.py`。

- **已删除的文件**：`review/deepseek.py`、`review/prompt.py`、`review/prompts/`
- **已移除的配置**：`config/review.py` 的 `DEEPSEEK_*` 常量；`config/secrets.py` 白名单
  与 `ENV_MAP` 中的 deepseek 三项
- **已移除的路由**：面板的 `POST /api/key`、`DELETE /api/key`
- **已改造的调用方**：`review/review_loop.run_review(records, force=)`（不再收 client）、
  `review/review_loop.annotate_error(rec, journal=, recent_stats=)`、
  `runtime/scheduler.DailyScheduler(journal=)`（不再收 get_client）

### 统计引擎的四条规则

| 规则 | 信号 | 动作 |
| --- | --- | --- |
| 维度权重 | 各面「赢单均值 − 错单均值」的**组内相对**判别力 | 高于组内均值加权、低于则减权 |
| 决策阈值 | 各档位实际胜率 | 胜率 < 45% 收紧、> 60% 放宽 |
| 安全阀 | 整体胜率 | 偏低则抬高开仓门槛 |
| 技术乘数 | 技术面判别力符号 | 反向判别时抑制 ADX / 布林收口乘数 |

全部阈值常量集中在 `review/statistical_review.py` 顶部，便于人工审阅与测试钉死。

### 与旧实现共享的安全边界（不要削弱）

建议**仍然**必须通过 `review_loop.validate_changes()`：
白名单 → 类型/有限性 → 条数上限 → 区间夹取 → 相对幅度 →
权重组归一（和恒为 1.0）→ 阈值不交叉。
只有 `accept_proposal()` 才会写新版本，旧版自动留档可回滚。

### 两条防自欺设计（不要"优化"掉）

1. **相对而非绝对** — 所有面读数普遍偏高只反映市场状态，不是调参信号。
   `test_relative_tilt_not_absolute_level` 专门钉死这一点。
2. **对称失效单独处理** — 带符号均值会正负相消；错单里读数幅度明显更大的面，
   即便判别力为正也会被打折（`EXTREME_MUTE_FACTOR`）。

### 如果将来想接回 LLM

不要直接改回 `run_review`。正确做法是新增一个「建议来源」实现，产出同样的
`{param, current, proposed, rationale, expected_effect, confidence}` 结构，
交给 `validate_changes()` —— 护栏层与建议来源无关。

## 7. 密钥与安全边界

本地凭证位于 `runtime/secrets.json`，已被 `.gitignore` 忽略。同样被忽略的还有 `runtime/review/positions.json`、`runtime/review/*.jsonl`、`.env`、`.manus/`、`.tmp-router/`。

必须遵守的规则：

1. **不要读取、打印、复制或提交 API Secret。**
2. 不要把密钥写进源码、README、日志、截图或 Git。
3. 只允许 Binance Futures Testnet：`https://testnet.binancefuture.com`。
4. 不要启用主网 URL，不要绕过 `runtime_mode.py`。
5. 任何真实 Testnet 下单前，必须先说明精确的方向、数量、订单类型和退出方式，并取得用户明确确认。
6. 用户此前在聊天里暴露过 `sk-a29d7e68...`（DeepSeek）、`pred_sk_e8fd22c2...`
   （Predict.fun）和 Binance Testnet Key/Secret，**这些 key 都应视为已泄露**。
   DeepSeek key 现已**完全不需要**（LLM 已移除），可直接作废；
   Binance Testnet key 与 Predict.fun key 需要重新生成。

---

## 8. Testnet 测试历史与当前状态

上一轮执行过一次用户确认的 Testnet 测试：`BTCUSDT`、`LONG`、`0.001 BTC`、限价入场，随后用 `reduceOnly MARKET SELL` 平仓。

过程中暴露了 §5.2 的状态机 bug，产生了遗留仓位，最终通过读取交易所真实状态后用 `reduceOnly` 平掉。核验结果：

```text
LONG 0.001 BTC  →  FLAT 0 BTC
```

当时的账户快照约为：钱包余额 4952.73 USDT、BTCUSDT 仓位 `FLAT`、数量 `0.0`。

**但请不要假设这个状态至今未变。** 接手后第一步应该调用 `/api/trading/status` 重新读取交易所真实状态。

当时状态里同时存在：

```json
{
  "exchange_position": {"symbol": "BTCUSDT", "side": "FLAT", "quantity": 0.0},
  "reconciliation_needed": true,
  "guardian": {"allow_new_entries": false}
}
```

即交易所已归零，但本地标志未清——这正是 §5.1 描述的问题。**在对账逻辑修复并验证前，不要重新启用自动开仓。**

---

## 9. 启动与测试命令

### 只启动前端预览

```bash
cd /Users/zengyun/我的AI/crypto
sh scripts/start_new_frontend.sh
```

访问 `http://127.0.0.1:8788/`。

### 启动完整本地控制台

```bash
cd /Users/zengyun/我的AI/crypto
TRADING_MODE=paper sh scripts/run_local_console.sh
```

该脚本（`scripts/run_local_console.sh`）会同时拉起后端 8787 和前端 8788，默认 `TRADING_MODE=paper`，并用 trap 在退出时清理子进程。

### 测试与语法检查

```bash
cd /Users/zengyun/我的AI/crypto
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test*.py'
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile review/panel_server.py trading/binance_client.py trading/executor.py trading/position_manager.py
node --check frontend/app.js
```

### 环境注意事项

- 本机 Python 为 3.9（Xcode 自带），依赖装在 `~/Library/Python/3.9`。
- 绑定 `127.0.0.1` 端口在某些沙箱环境下会被本地执行策略拒绝，需要单独申请权限。
- 测试套件包含 `tests/test_portfolio_manager.py`、`tests/test_review_guard.py`、`tests/test_round2_acceptance.py`、`tests/test_round3_acceptance.py` 等，修改 `position_manager.py` 或 `scorer.py` 后必须全量回归。

---

## 10. 建议执行顺序

请严格按顺序推进，不要跳步：

1. **审计阶段。** 读取 `.gitignore` 和 `runtime/secrets.json` 的字段名（不要读值），确认没有密钥进入 Git。检查 `git status` 确认工作区范围正确。
2. **状态确认。** 以 `paper` 模式启动，读取 `/api/health` 和 `/api/trading/status`，记录交易所真实余额与仓位。
3. **修复对账（§5.1）。** 实现 `reconcile_now()`、新增 `/api/trading/reconcile`、让 Testnet 接口和密钥重绑后触发对账。为它写单元测试。
4. **修复限价单状态机（§5.2）。** 用 `FakeBinanceClient` 覆盖成交、部分成交、撤单、超时后实际成交四种场景。
5. **加固保护单（§5.3/§5.5）。** 验证 `STOP_MARKET` / `TAKE_PROFIT_MARKET` / `reduceOnly` / `closePosition` 的组合在 Testnet 上真实可用，校验 tick/lot 精度，保护单失败必须阻断后续自动交易。
6. **修复指纹漂移（§5.3）。**
7. **统一错误返回（§5.4）。**
8. **Paper 全闭环验收。** 开仓 → 保护单 → 触发 → 平仓，全程走 Paper。
9. **仅在用户再次明确确认后**，执行极小额 Testnet 验证（建议 0.001 BTC）。
10. **前端完善。** 在深色模式基础上补充对账告警、订单状态机可视化、逐笔盈亏和交易日志。

### 每完成一个阶段后必须做

回到 `docs/PROJECT_MILESTONES.md`，把本次完成的 DoD 条目勾选，更新对应里程碑的完成度百分比，并在文末"变更记录"追加一行（日期 + 变更 + 测试结果）。这是项目的进度基线，不能只改代码不更新进度。

---

## 11. 验收标准

全部满足才算完成：

- Paper 模式默认安全启动，主网路径仍被锁定。
- Testnet 余额、仓位、订单均从交易所 API 实时读取，不用假数据冒充真实状态。
- 限价单不会因网络超时被误判为未成交。
- 成交后自动建立止盈与止损保护；保护单失败时禁止继续交易。
- 交易所仓位与本地策略账本可双向对账，且提供手动"立即对账"入口。
- 自动平仓后交易所与本地**同时**显示 `FLAT / 0 BTC`。
- 每笔交易记录入场价、退出价、手续费、已实现盈亏和退出原因。
- 所有交易 API 错误返回结构化 JSON，且不含密钥。
- 前端保持深色交易终端风格，不添加 K 线图。
- 全量测试通过，语法检查通过。
- Git 提交只包含 `crypto/` 目录内容，不含密钥、运行时账户状态或上级目录无关文件。

---

## 12. 立即行动

请先只做第 1 步和第 2 步，把**当前真实状态**和**审计发现**报告出来，然后再开始改代码。

**不要直接下单。**
