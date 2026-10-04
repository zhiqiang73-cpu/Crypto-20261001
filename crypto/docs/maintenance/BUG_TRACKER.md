# Crypto 调试与 Bug 维护备忘

> 维护人：New Bot（本机协作）  
> 项目：`/Users/zengyun/我的AI/crypto`  
> 前端：`http://127.0.0.1:8788/` · 面板通常 `8787`  
> 策略线：BTC/ETH 15m KDJ×MACD 分层（用户口中的 `deploy/kdj-macd-pyramiding-20261003`）  
> 运行态：`runtime/shadow/` · 代码：`shadow/deploy.py`  
> 更新：2026-10-04 10:55 CST

## 当前实况（抽样）

| 项 | 状态 |
|---|---|
| trader heartbeat | `running` / `testnet_orders` / 策略 `kdj15`+`eth15` |
| 前端 8788 | 在跑（`http.server` → `frontend/`） |
| 账本（state） | BTC 多 0.181 · ETH 多 9.567（以 `deployed_state.json` 为准） |
| 今晨修复迹象 | 日志已出现「保证金不足跳过」「账本回滚」「幽灵仓位清零」——B1/B2/B3 似已部分落地，仍需回归确认 |

主要证据：
- 值班总结：`outputs/duty-watch-20261003/总结-20261004-0600.md`
- 值班日志：`outputs/duty-watch-20261003/log.md`
- 修复冒烟：`outputs/fix-verify-20261004/`

---

## Bug 清单

| ID | 严重度 | 状态 | 位置 | 现象 | 备注 |
|---|---|---|---|---|---|
| B1 | 🔴 | 疑似已修·待回归 | `shadow/deploy.py` sync_net | 单笔委托失败拖垮整 tick → 连环中断 | 昨晚根因；今晨日志不再见空 `异常:` 风暴，需确认永不抛契约 |
| B2 | 🔴 | 疑似已修·待回归 | 仓位定量 | 无保证金/杠杆可行性校验 → -2019 刷屏 | 已见「净仓跳过 / 保证金不足」 |
| B3 | 🔴 | 疑似已修·待回归 | `apply_virtual_signal` | 下单失败仍按已开仓记账 | 已见「账本回滚」「幽灵仓位清零」 |
| B4 | 🟠 | 开放 | `trading/binance_client.py` | 失败委托无限重试（25–27 次/tick） | 与 B1/B2 联动 |
| B5 | 🟠 | 开放 | 停机补记路径 | 错过信号补记但不执行、无告警 | 昨晚 ETH 漏 3 信号含 2 次反手 |
| B6 | 🟠 | 开放 | `save_state` | 落盘 state 滞后甚至方向相反 | 影响面板与重启恢复 |
| B7 | 🟡 | 开放 | 异常处理 | `异常: ` 空消息，难排障 | |
| B8 | 🟡 | 开放 | panel `/api/trading/status` | 未实现盈亏恒为 0 | |
| B9 | 🟡 | 开放 | trader 日志 | 无轮转；含旧路径噪音 | |

### 非 bug（勿重复报）

- 「金叉但 MACD 红柱」按本仓库约定丢弃（绿柱>0 / 红柱<0）
- 钱包小幅跳变可能是资金费结算

---

## 待用户决策（来自 10-04 06:00 值班）

1. ETH 若仍处错误方向/过高保证金占用时，是否人工处理（以当下交易所为准）
2. 是否正式验收并锁定 B1–B3 修复
3. 是否修 B4–B6
4. 是否恢复 `BLOCK_ON_DAILY_LOSS` / `HALT_ON_MAX_DRAWDOWN`
5. 是否实现 1.5×ATR 常规止损 + 保本止损（目前仅有 3×ATR 灾难止损）
6. 是否删除过期整点巡检 cron `89ee1209`

---

## 运维备忘

- Git 根在上一级 `/Users/zengyun/我的AI`；提交只用 `git add -A .`（在 crypto 内）
- 备份仓：`https://github.com/zhiqiang73-cpu/Crypto-20261001`（可能落后于本机）
- 改 `config/strategy_bundle.py::_IMPL_FILES` 内文件后须 `python3 -m scripts.reseal_strategy --reason "..."` 
- 进度基线：`docs/PROJECT_MILESTONES.md`
- 交接：`CLAUDE_HANDOFF_PROMPT.md`
- 安全边界：仅 Paper/Testnet，**禁止主网**

---



## 实盘核对快照 · 2026-10-04 10:56 CST

| 项 | 交易所 | 账本/策略 |
|---|---|---|
| BTC | LONG 0.1809 @84816.09 · 保证金≈1526 · 浮亏≈-1.93 | kdj15 多 0.181 · 1/3层 · 金叉做多 02:00Z |
| ETH | LONG 9.566 @2687.95 · 保证金≈2605 · 浮盈≈+39.22 | eth15 多 9.567 · 1/3层 · 金叉做多 00:15Z |
| 钱包/可用 | 4349.16 / **255.23（≈5.9%）** | peak/day_start≈4419 · halted=false |
| 心跳 | trader `running` testnet_orders | missed_bars/signals=0 |
| 方向一致性 | **一致（双多）** | 02:30 ETH 死叉但绿柱已正确丢弃 |
| 风险 | 保证金占用约 **95%**，几乎加不动层；非昨夜错误空仓 | 10:04 有历史清零重启记录 |

结论：方向正常、账本与交易所对齐；主要风险是可用保证金过低，不是方向错误。

## 变更记录

| 时间 | 事项 |
|---|---|
| 2026-10-04 11:03 CST | RISK_R 0.03→0.01（engine+4策略卡+治理文档+相关测试）；现仓不动；需重启 trader 才生效 |
| 2026-10-04 10:55 CST | 从值班总结归档 B1–B9；对照今晨日志标注 B1–B3 疑似已修 |
| 2026-10-04 10:56 CST | 核对测试网：双多对齐；可用仅≈255（占用~95%） |