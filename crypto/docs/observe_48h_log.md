# BTC 四面交易系统 · 48h 只读观察日志

- **开始**：2026-09-15 22:31 CST
- **计划结束**：2026-09-17 22:31 CST
- **巡检间隔**：每 2 小时
- **规则**：只观察、只记录、**不改代码/配置/仓位**
- **判定基准**（系统设定）：
  - 开仓：`STANDARD_*` / `STRONG_*`（`|CS|≥20` / `|CS|≥45`）
  - 观望：`WATCH_*`（约 `|CS|` 在 8–20）→ **不应新开仓**
  - 中性：`|CS|<8` → 不应新开仓；持仓可按出场规则平/减
  - 杠杆：短 2x / 长 3x；对账阻塞时不应新开
- **面板**：http://127.0.0.1:8787/
- **你明早检查点**：2026-09-16 07:00 CST

---

## T0 · 2026-09-15 22:31 CST（基线）

| 项 | 值 |
|---|---|
| 进程 | `review.panel_server` 在跑（pid≈47961，:8787） |
| trading_enabled | true |
| trading_connected | true |
| reconciliation_needed | false |
| 本地仓位 | short=FLAT / long=FLAT |
| 交易所仓位 | FLAT qty=0 |
| 钱包 | ≈4983.17 USDT，未实现盈亏 0 |
| mark（guardian） | ≈76327，age≈3s，health=normal |
| CS（live） | **-11.7** → **WATCH_SHORT** |
| 门槛（live） | long +20 / short −20；watch ±8 |
| 四面摘要 | 消息 −3.4 / 数据 −3.3 / 技术 −9.1 / 预测 −62.8 |
| allow_new_entries | true |
| trading/status.error | `invalid_data_record:mark_price:stale`（有记录，本轮不修） |
| history 行数 | 7（含早前开平 + 22:21 孤儿手动平仓） |
| trade_ledger | 0 行（清空后尚未新成交） |

### 开平仓合规（相对基线以来的新成交）

- **本窗口新开仓**：无
- **本窗口新平仓**：无（孤儿平仓在观察窗开始前）
- **是否符合设定**：是。CS=−11.7 属 WATCH_SHORT，低于 standard_short(−20)，**不开仓正确**。

### 备注

- 历史里仍可见今晚更早的 STANDARD_LONG / STANDARD_SHORT 等记录；观察窗以本 T0 之后的增量为准。
- 下一 tick：约 2026-09-16 00:31 CST。

---

## T1 · 2026-09-16 00:32 CST

| 项 | 值 |
|---|---|
| 进程/健康 | health.ok=True full_cs=True |
| trading | enabled=True connected=True recon=False |
| 仓位本地 | short=None / long=None |
| 交易所 | FLAT qty=0.0 |
| 钱包 | 4983.16530086 USDT · uPnL=0.0 |
| CS / 决策 | **-9.6** → **WATCH_SHORT**（门槛 long 20 / short -20） |
| 四面 | 消息面 12.1, 数据面 -4.2, 技术面 -11.2, 预测面 -52.5 |
| guardian | health=normal mark=76419.44300576 age=6.7s allow_new=True |
| status.error | `invalid_data_record:mark_price:stale` |
| history | 文件 7 行（较上 tick Δ0）；观察窗新成交 0 笔 |

### 开平仓合规

- **本窗口新开仓**：无
- **本窗口新平仓**：无
- **是否符合设定**：是
- 备注：观望/中性且空仓，不开仓正确

---

## T2 · 2026-09-16 02:32 CST

| 项 | 值 |
|---|---|
| 进程/健康 | health.ok=True full_cs=True |
| trading | enabled=True connected=True recon=False |
| 仓位本地 | short=None / long=None |
| 交易所 | FLAT qty=0.0 |
| 钱包 | 4983.16530086 USDT · uPnL=0.0 |
| CS / 决策 | **-7.3** → **NEUTRAL**（门槛 long 20 / short -20） |
| 四面 | 消息面 10.8, 数据面 -4.2, 技术面 -9.0, 预测面 -36.1 |
| guardian | health=normal mark=76827.8 age=5.1s allow_new=True |
| status.error | `invalid_data_record:mark_price:stale` |
| history | 文件 7 行（较上 tick Δ0）；观察窗新成交 0 笔 |

### 开平仓合规

- **本窗口新开仓**：无
- **本窗口新平仓**：无
- **是否符合设定**：是
- 备注：观望/中性且空仓，不开仓正确

---

## T3 · 2026-09-16 04:32 CST

| 项 | 值 |
|---|---|
| 进程/健康 | health.ok=True full_cs=True |
| trading | enabled=True connected=True recon=False |
| 仓位本地 | short=None / long=None |
| 交易所 | FLAT qty=0.0 |
| 钱包 | 4983.16530086 USDT · uPnL=0.0 |
| CS / 决策 | **-2.6** → **NEUTRAL**（门槛 long 20 / short -20） |
| 四面 | 消息面 6.5, 数据面 8.0, 技术面 -0.6, 预测面 -62.8 |
| guardian | health=degraded mark=75809.39062835 age=3.2s allow_new=True |
| status.error | `stale:polymarket=19278s>600s` |
| history | 文件 7 行（较上 tick Δ0）；观察窗新成交 0 笔 |

### 开平仓合规

- **本窗口新开仓**：无
- **本窗口新平仓**：无
- **是否符合设定**：是
- 备注：观望/中性且空仓，不开仓正确

---

## T4 · 2026-09-16 06:32 CST（明早 07:00 检查点前最后一拍）

| 项 | 值 |
|---|---|
| 进程/健康 | health.ok=True full_cs=True |
| trading | enabled=True connected=True recon=False |
| 仓位本地 | short=None / long=None |
| 交易所 | FLAT qty=0.0 |
| 钱包 | 4983.16530086 USDT · uPnL=0.0 |
| CS / 决策 | **4.0** → **NEUTRAL**（门槛 long 20 / short -20） |
| 四面 | 消息面 8.2, 数据面 8.4, 技术面 -15.8, 预测面 27.7 |
| guardian | health=degraded mark=75553.12932971 age=1.7s allow_new=True |
| status.error | `stale:polymarket=26487s>600s` |
| history | 文件 7 行（较上 tick Δ0）；观察窗新成交 0 笔 |

### 开平仓合规

- **本窗口新开仓**：无
- **本窗口新平仓**：无
- **是否符合设定**：是
- 备注：观望/中性且空仓，不开仓正确

### 过夜汇总（T0→T4，供 07:00 查阅）

- 巡检：T0 22:31 / T1 00:32 / T2 02:32 / T3 04:32 / T4 06:32
- 仓位：全程 FLAT（观察窗内无新开平）
- 决策轨迹：WATCH_SHORT → WATCH_SHORT → NEUTRAL → NEUTRAL →（本拍见上）
- 合规：观察窗内开仓规则均满足（未达 ±20 不开）

---
