# 任务：实盘系统 · 监控页修复与精简 + 行情腿对齐主网（2026-10-05）

## 工作目录
- 主目标（实盘系统克隆）：`/Users/zengyun/我的AI/crypto/.work/rt-dev/crypto`
- 镜像目标（老系统同款前端，保持 diff=0 的既有约定）：`/Users/zengyun/我的AI/crypto/frontend/`

面板运行在 http://127.0.0.1:8787/ ，前端文件在 `frontend/`（index.html / app.js / styles.css / chart.js）。

## 背景（已由外部核实，直接采信，不必重新排查）

这是**实盘系统**（主网真实资金）：
- 运行器（`shadow/deploy.py --execute`，pid 95401）以 `TRADING_MODE=live` + `CONFIRM_MAINNET=YES_I_UNDERSTAND` 运行，行情腿与下单腿都是主网（fapi.binance.com / fstream.binance.com），真实下单已启用。
- **面板服务（com.crypto.live.panel）的 launchd 配置缺 `TRADING_MODE` / `CONFIRM_MAINNET` 环境变量**，导致面板进程里 `current_mode()` 退回 `paper`：
  1. 面板自己的行情采集器（collectors/binance_ws.py 经 config/mapping.py `_MARKET = resolve_for_account()`）连到了**测试网** WS（`wss://fstream.binancefuture.com`），`/api/market` 返回 `market="testnet"`，页面「监控 → 行情链路」显示测试网价格；
  2. 面板的交易客户端 `BinanceTestnetClient()` 默认读 `binance_testnet_*` 凭据，而本克隆只有 `binance_mainnet_*` 凭据 → `configured=False` → 账户/持仓/委托/成交数据全空；
  3. `/api/chart`（review/chart_data.py 经同一解析器）也在取测试网 K 线；
  4. `/api/config` 的 `runtime.mode` 显示 `paper`、`mainnet_execution_enabled` 恒为 `false`；前端设置页还硬编码显示「主网执行 已禁用 / runtime_mode 硬阻断」，与实况完全相反，用户被误导为"系统禁止交易"。
- 用户三个诉求：
  1. 监控页「行情链路」不得再显示测试网价格，要看真实（主网）数据；
  2. 监控页太乱，精简为只回答 6 个问题：**系统是否在运行 / 各功能是否正常 / 数据是否真实流动 / 账本是否正确 / 执行与保护是否正确 / 风控是否实现**；
  3. 设置页「主网执行」文案要反映真实状态（当前实际已启用双重确认、真实资金下单）。

## 要求 A：面板服务环境对齐（部署配置，只改仓库文件）

修改 `/Users/zengyun/我的AI/crypto/.work/rt-dev/crypto/scripts/launchd/com.crypto.live.panel.plist`：
- 在 `EnvironmentVariables` 字典里补两项（与同目录 `com.crypto.live.trader.plist` 一致）：
  - `TRADING_MODE` = `live`
  - `CONFIRM_MAINNET` = `YES_I_UNDERSTAND`
- 其余内容不动。
- **不要**改 `~/Library/LaunchAgents/` 下的文件，**不要**执行 launchctl / 重启任何服务（安装与重启由外部执行；你只负责仓库文件）。在报告里明确写出「本项需外部重启面板服务后生效」。

## 要求 B：后端小改动

### B1. 运行器心跳补充字段（shadow/deploy.py）
找到写 `runtime/shadow/runner_heartbeat.json` 的位置（搜 `params_fingerprint`），在心跳 JSON 里**新增两个字段**（只加字段，不改任何现有行为）：
- `"trading_mode": current_mode().value`（live / testnet / paper；该函数已在该文件 import）
- `"mainnet_confirmed": mainnet_confirmed()`（布尔；已 import）
注意：当前运行中的进程是旧代码，这两个字段要等运行器下次重启才会出现——**前端不得依赖这两个字段才能正确显示**（见 C3）。

### B2. review/panel_server.py

**(a) `/api/config` 的 `mainnet_execution_enabled` 改为动态计算**
- 读取 `runtime/shadow/runner_heartbeat.json`（面板已有读该文件的先例，见 api_monitor 里的 read_json 模式；这里自己写一个小 helper，注意容错）。
- 判定规则：心跳文件存在、`updated_ms` 距当前 < 90 秒（新鲜）、且 `market == "mainnet"`、且 `mode` 不属于 `{"observe","observation_only"}` → `mainnet_execution_enabled = True`；否则 `False`。
- 同时新增 `"mainnet_execution_note"`：一句话中文说明（例如「运行器心跳确认：主网真实资金下单已启用（双重确认）」/「运行器未运行或非主网模式」）。
- 保留其它字段不动（`runtime`、`mainnet_configured` 等保持原样；可以顺手把第 697 行附近"主网执行仍由 runtime_mode 硬阻断"的过时注释改准确）。

**(b) 安全加固：两个 `/api/testnet/*` 端点加市场闸门（重要）**
面板切到 live 后，`review.executor.client` 会拿到**主网**客户端；以下两个端点名叫 testnet 但当前**没有任何市场检查**，一旦被调用会在主网下真实订单：
- `/api/testnet/smoke-limit-close`（api_testnet_limit_then_close）
- `/api/testnet/flatten-now`（api_testnet_flatten_now）
要求：在两个函数开头加闸门——用 `config.market_endpoints.market_of_url(client.base_url)`（或等价手段）判断客户端市场；**非 testnet 时直接 400 拒绝**，返回清晰中文错误（如 `{"ok": False, "error": "testnet_only_endpoint", "detail": "该端点仅限测试网；当前客户端市场=mainnet，已拒绝执行"}`）。加在 `client.configured` 检查之后、任何下单动作之前。

**(c) 顺手改注释**：`api_chart` 的 docstring「（只读，仅测试网）」改为准确描述（行情地址由账户反推，主网/测试网跟随运行模式）。

不要改 trading/、shadow/engine.py、策略卡、风控核心逻辑。

## 要求 C：前端（rt-dev/frontend/，改完镜像到主目录 frontend/）

所有新读取的字段必须**空值安全**（旧后端可能没有这些字段，例如老系统的 /api/config 没有 mainnet_execution_note、心跳没有 trading_mode；取不到时优雅回退，绝不能抛错中断脚本）。

### C1. 顶栏模式徽标 + 底部 footer 动态化
- 新增一个通用函数（例如 `modeZh(mode, market)`），规则：
  - market：`mainnet`→「主网」、`testnet`→「测试网」、其他→「未知市场」；
  - mode：`testnet_orders`/`execute` → `${market} · ${主网? "真实下单" : "实盘下单"}`；`observe`/`observation_only` → `${market} · 只观察`；`paper` → 「纸面 · Paper」；空 → 「模式未上报」。
- 顶栏 `modePill`（renderTopbar 里现在写死 "测试网 · 实盘下单"）改用该函数；市场取 `runnerInfo().market`（心跳里已有 `market` 字段，主网时是 `"mainnet"`）。
- 底部 footer「BTCUSDT + ETHUSDT · USDⓈ-M 永续 · 币安测试网」里的「币安测试网」改为动态（用 marketCache.market_label / market 渲染，如「币安主网 (mainnet)」「币安测试网 (testnet)」）；给该 span 加 id，由 app.js 渲染；取不到时保留原文案。

### C2. 监控页「行情链路」修正
- 「行情通道」行的 `x` 文案去掉硬编码「测试网 K 线」：按 `rd.kline_url` 的 host 动态判断——含 `testnet.binancefuture.com` 或 `demo-fapi` → 「测试网 K 线」；含 `fapi.binance.com` → 「主网 K 线」；否则「来源未知」。
- **新增一行「行情一致性」（本次故障哨兵）**：面板采集器市场（`marketCache.market`）vs 运行器市场（`monitorCache.heartbeat.market`）——两者都已知且相等 → `<span class="up">一致</span>`，x 写「面板采集器 = 运行器」；不等 → `<span class="down">不一致</span>`，x 写 `面板 ${panelMarket} ≠ 运行器 ${runnerMarket}`，行加 `is-bad`；任一未知 → `—`。
- 其余行保留。

### C3. 设置页「主网 API 凭据」区（renderMainnetConfig 及相关）
- 「主网执行」行改为动态，数据源优先 `monitorCache.heartbeat`（心跳已含 market/mode）：
  - 心跳新鲜（<90s）且 market=="mainnet" 且 mode 非 observe → `v: <span class="up">已启用（双重确认）</span>`，`x: "TRADING_MODE=live + CONFIRM_MAINNET · 真实资金"`；
  - 心跳新鲜但不满足上述 → `v: <span class="warn">已禁用（安全闸门）</span>`，`x: "缺任一确认即阻断"`；
  - 心跳不新鲜/缺失 → `v: 未运行`，`x: "运行器未运行，不产生下单"`；
  - 若心跳里没有 market 字段（旧后端），回退用 `cfg.mainnet_execution_enabled` / `cfg.runtime`。
- `mainnetConnection` 大字横幅：已保存时由「已保存（执行仍禁用）」改为动态——执行已启用 →「已保存 · 执行已启用」，否则「已保存 · 执行未启用」；未配置 →「未配置」。
- 更新该区的静态文案，使其不再暗示"主网被硬阻断"：
  - 「上线前必读」warn-box：保留密钥安全要点（专用 Key、只读+合约交易、禁提现划转、IP 白名单），标题可改为「密钥安全须知」；
  - 4 步 `mn-steps`：改写为现状描述——启用机制 = 双重确认（`TRADING_MODE=live` + `CONFIRM_MAINNET=YES_I_UNDERSTAND`，缺一即阻断）；保存/测试不会自动启用；当前状态看上方横幅；回滚 = 停运行器 → 改回 testnet 或删除凭据。措辞准确、简短。
- 删除凭据的 confirm 文案（app.js 约 1465 行「测试网凭据与运行器不受影响」）改为准确说明：「仅删除本机保存的主网凭据；正在运行的运行器不受影响，但重启后将因缺少凭据拒绝启动。」
- 检查其它残留的过时「测试网」硬编码文案（app.js 1533 行附近的只读测试提示、1590 行附近的测试单 prompt 等）：若对应元素在 index.html 中已不存在（死代码），保持空守卫即可，不要引发报错；若仍可见，改成与当前市场一致的措辞。

### C4. 监控页精简（按用户 6 问重组）

在 `frontend/index.html` 的 `#view-monitor` 与 `app.js` 的 `renderMonitorView()` 上做以下增删（每个删除都要在 app.js 里同步删掉渲染代码或加空守卫）：

1. **删除「巡检快照」面板**（msnapBox / msnapMeta 相关 section 与渲染代码）。理由：该面板显示 monitor.log 尾部原始日志，实盘克隆里该文件为空、事件日志已覆盖。
2. **「数据覆盖与成本」改名「数据与成本核对」，删统计类行**：
   - 删除：窗口净额、开仓名义、单笔边际、胜率 / 盈亏比、平均盈亏、权益记录点；
   - 保留：接口覆盖、统计窗口、起点裁剪、孤儿平仓、费用·Maker、费用·Taker、资金费、滑点估算。
3. **「账本对账」删除「旧对账标记」行**。
4. **「风控闸门」删除「记录起点」「5m 策略」两行**。
5. **新增「系统自检」横条**（放在 `#view-monitor` 最顶部，单行、紧凑、6 项，复用现有状态函数与 CSS 类，视觉与现有 .health-list/.hl 或 .pill 风格一致）：
   - ① 运行器（heartbeatState）、② 行情数据（marketState + 行情一致性）、③ 风控（runnerRisk：halted/日亏/回撤）、④ 执行与保护（protectionState + guardian）、⑤ 账本（reconState）、⑥ 数据核对（coverage：接口覆盖笔数/是否截断）。
   - 每项 = 状态圆点（绿/黄/红）+ 名称 + 一句话状态（≤10 字）。数据一律取自现有状态缓存，不新增请求。
6. 其它面板（运行器 / 行情链路 / 风控闸门 / 执行与保护 / 账本对账 / 事件日志）除上述行级修改外，结构保持不变。
7. 「运行器」面板的「模式」行：v 改用 C1 的 `modeZh()`，x 改为动态提示（execute →「execute：按信号真实下单」；observe →「只记账不下单」）。
8. 注意 index.html 的 `<section>` 配平、id 不重复；删除面板后 app.js 对应 `$("...")` 必须有空守卫。

### C5. 镜像同步
改完后把 rt-dev 的 `frontend/index.html`、`frontend/app.js`（若改了 styles.css 也一并）复制到 `/Users/zengyun/我的AI/crypto/frontend/`，保持两处逐字节一致（`diff -rq` 无输出）。

## 约束（务必遵守）

- 只改这些文件：
  - `.work/rt-dev/crypto/scripts/launchd/com.crypto.live.panel.plist`
  - `.work/rt-dev/crypto/shadow/deploy.py`（仅 B1 心跳字段）
  - `.work/rt-dev/crypto/review/panel_server.py`（仅 B2 范围）
  - `.work/rt-dev/crypto/frontend/{index.html,app.js,styles.css}`
  - `/Users/zengyun/我的AI/crypto/frontend/{index.html,app.js,styles.css}`（镜像）
- **不要重启/停止/重载任何服务**（面板与运行器都在运行中）；不要碰 `~/Library/LaunchAgents/`；不要执行 launchctl。
- 不要执行 `git add` / `git commit` / 任何写 `.git` 的操作。
- 不要动 `trading/`、`shadow/engine.py`、`config/`、`runtime/secrets.json`、策略卡、测试目录。
- 不改颜色体系、不改布局框架、不引入新依赖。
- 前端改动对缺失字段必须容错（老系统后端没有新字段时页面不能报错）。

## 完成后自检（把证据写进最终回复）

1. `plutil -lint scripts/launchd/com.crypto.live.panel.plist`（应 OK）。
2. `python3 -m py_compile shadow/deploy.py review/panel_server.py`。
3. `node --check frontend/app.js`（本机若无 node，说明并跳过）。
4. `grep -n "测试网" frontend/app.js frontend/index.html`：列出全部残留并逐条说明——动态判断分支里的「测试网」字样属正常；硬编码展示文案必须已消除。
5. `grep -n "msnapBox\|msnapMeta\|巡检快照" frontend/index.html frontend/app.js`：应无结果或仅剩注释。
6. `diff -rq frontend /Users/zengyun/我的AI/crypto/frontend`：应无输出。
7. 统计 index.html 中 `<section` 与 `</section>` 数量配平。
8. 快速回归（若本机 python 环境可用，且耗时 <3 分钟）：`python3 -m pytest tests/test_live_wiring.py tests/test_market_endpoints.py -q`；不可用或失败请如实说明原因，不要掩盖。

## 最终回复格式

- 改了哪些文件、每个文件的关键改动摘要；
- 上面 8 项自检的实际输出/结论；
- 明确写一句：**面板 plist 的 env 变更需外部重启 com.crypto.live.panel 服务后才生效**；
- 遗留问题与建议（如有）。
