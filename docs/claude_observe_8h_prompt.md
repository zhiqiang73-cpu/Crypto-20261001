# Claude 提示词：BTC 四面交易系统 · 8 小时只读观察

把下面整段复制给 Claude（Desktop / Cowork / Code 均可）。

---

你在本机仓库工作：

`/Users/zengyun/Downloads/我的AI/crypto`

这是 BTCUSDT「四面评分 → 短/长期决策 → Binance Futures（测试网）自动交易」系统。

## 你的任务

1. **启动系统**（面板 + 交易链路），确认健康后进入观察。
2. **连续观察 8 小时**；**每 1 小时**巡检一次并写入日志。
3. **只观察、只记录、不修改**：不改代码、不改配置、不改 ACTIVE、不改仓位、不下手动单、不读/不打印密钥与 secrets。
4. 8 小时结束后，给主人一份**总结报告**（中文、结论先行）。

## 启动步骤

```bash
cd "/Users/zengyun/Downloads/我的AI/crypto"
# 确认 8787 空闲
lsof -tiTCP:8787 -sTCP:LISTEN && echo busy || echo free
python3 -m review.panel_server
```

面板：http://127.0.0.1:8787/

启动后：
- 用 API 打开交易开关（若默认关）：`POST /api/trading/toggle` body `{"enabled": true}`
- 确认：`GET /api/health` → ok、trading_configured、trading_connected、trading_enabled
- **不要**切换 `config/weights_versions/ACTIVE.json`（当前应为 v1）

只读快照脚本（已有）：

```bash
"/Users/zengyun/Downloads/我的AI/crypto/scripts/observe_tick_snapshot.sh"
```

人工日志追加到：

`docs/observe_8h_claude_log.md`（若不存在则新建；不要动 runtime 账本去“洗数据”）

原始 JSONL 可继续追加到 `docs/observe_8h_claude_raw.jsonl`（或沿用 snapshot 脚本输出路径，但请在报告里写明）。

## 当前生效规则（观察口径，勿擅自改）

ACTIVE = **v1**（与面板/引擎应一致）：
- 短期四面权重：**15 / 45 / 25 / 15**（消息/数据/技术/预测）
- 开仓门槛：**STANDARD ±35**，STRONG ±60/±65；WATCH 约 ±10
- 仓位：本地 short_term / long_term 两本账；交易所净仓合并（现状）
- 定仓：`qty = (权益 × risk_pct) / (ATR × hard_sl_atr)`；短风险 0.5%、长 1%
- `full_cs=true` **不等于**可交易；还要看 tradable、staleness、guardian、pretrade

判定开仓是否合规：
- 仅 `STANDARD_*` / `STRONG_*` 才应新开
- `WATCH_*` / `NEUTRAL` **不应**新开
- 若有开仓：核对当时 CS 是否达到门槛、方向是否一致、杠杆短 2x / 长 3x

## 每小时必须记录（表格即可）

时间（CST）、进程是否在线、enabled/connected/recon、  
CS + decision + config_version + 门槛、四面分、  
本地仓 / 交易所仓、钱包与 uPnL、  
guardian health / mark / allow_new_entries、  
status.error / 主要阻断原因、  
本小时是否有新开/平/减仓（对照 history 增量）、  
**合规：是/否 + 一句话理由**

可选：`decision_audit` 若有新行，摘要 signal_state / primary_block（不要推断没有记录的时段）。

## 硬性禁止

- 不改代码、权重、ACTIVE、密钥、杠杆、账户设置
- 不提交真实/测试网订单（除系统自动交易外，禁止手动 flatten/close/加仓）
- 不清空 journal / history / ledger / positions 来“制造好看结果”
- 不要用 1 个观察点推断“全程如何”；没有审计记录的时段标 **采样空白**
- 不得把“系统在跑 / 没报错 / 没开仓”写成“策略有效”或“适合实盘”

## 8 小时结束报告必须分栏

1. **可用性**：是否持续在线、几次中断  
2. **开平仓清单**：时间、horizon、方向、CS、decision、是否合规  
3. **决策分布**：NEUTRAL / WATCH / STANDARD / STRONG 大约占比（有审计用审计；否则注明仅小时采样）  
4. **阻断原因 Top**：stale / recon / pretrade / 未达门槛 等  
5. **异常**：进程挂掉、配置不一致、面板与引擎门槛不一致、孤儿仓等  
6. **结论四栏（必填，禁止合并措辞）**  
   - 工程运行是否按规则：通过 / 不通过 / 证据不足  
   - 测试网执行观察：有成交 / 无成交  
   - 策略优势：证据不足（除非另有授权回放，本任务不做）  
   - 是否建议继续实盘：不建议 / 仅继续观察  

## 时间安排建议

- T0：启动 + 基线（立刻写第一篇）  
- 之后每整点或每满 60 分钟一篇（共约 9 篇含 T0，或 8 次间隔以满 8 小时为准）  
- 结束：总结报告写入 `docs/observe_8h_claude_report.md`，并在对话里用中文给主人看摘要  

主人可能中途来看；保持日志文件随时可读。Cursor/会话需保持可用以便按时巡检；若环境会休眠，在日志中如实记录缺口。

开始工作：先启动系统 → 写 T0 → 进入每小时循环。

---
