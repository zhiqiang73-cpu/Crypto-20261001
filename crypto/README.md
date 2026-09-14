# BTC/USDT 四面一体量化交易系统 (Four-Dimension Trading System)

## 一、 项目背景与核心理念

本项目基于**「四面一体」**分析框架，构建专门针对 BTC/USDT 的量化分析、实时评分与自动化交易决策系统。

我们认为：BTC/USDT 的价格变动可以被四个正交维度完整解释，遗漏任何一个面都会导致模型盲区：
```
外部世界发生了什么？  →  消息面 (News): 事实与宏观催化剂 (长期流动性驱动 / 日内事件点火)
市场参与者做了什么？  →  数据面 (Data): 行为痕迹与清算引擎 (链上筹码 / 衍生品杠杆 / 订单簿微观结构)
价格本身表现了什么？  →  技术面 (Tech): 价格空间与阻力最小路径 (VPVR / 市场结构 / VWAP / 锚点)
市场押注未来会怎样？  →  预测面 (Prediction): 概率定价与预期差 (Predict.fun 5/15min 涨跌 / Polymarket 月度 / Fear&Greed / 期权引力)
```

---

## 二、 双仓位架构定位

系统采用**杠铃式双仓位配置**，两套仓位独立计算评分与风控：
1. **长期核心仓位 (月级别 / 现货为主)**：
   * 权重：消息面 35% + 数据面 30% + 预测面 20% + 技术面 15%
   * 核心驱动：全球 M2 / 美联储降息周期、ETF 累计净流入、MVRV 顶底估值、期权 IV 期限结构。
2. **短期战术仓位 (日内 / 永续合约)**：
   * 权重：数据面 45% (核心) + 技术面 25% + 消息面 15% + 预测面 15%
   * 核心驱动：清算热力图磁铁效应、资金费率极端值、OI 骤增/出清断崖、CVD 顶底背离、订单簿深度失衡。

---

## 三、 核心理论手册：`btc_factor_classification.md` (V7 版)

项目根目录下的 `btc_factor_classification.md` 是全系统的**理论白皮书与规则宪章**。
当前版本为 **V7（双向统一评分体系版）**，核心突破在于：
* **从单向 [0, 100] 全面升级为双向统一 [-100, +100]**：
  * `+100` = 极度看多，`-100` = 极度看空，`0` = 中性/无方向。
  * 一次计算同时输出**交易方向 (LONG / SHORT / NEUTRAL)** 与 **置信强度**，彻底解决做空难以归一化的问题。
* **所有指标具备双向量化规则**：每个指标从原始值映射到 [-100, +100] 的客观分值。
* **多空不对称决策阈值**：针对 BTC 长期上涨趋势，做空阈值绝对值设定比做多高 5 分。
* **维度安全阀 (Safety Valve)**：任一维度与综合方向严重矛盾（差值 > 50）自动降级避险。
* **黑天鹅覆盖规则 (Black Swan Override)**：全网巨额爆仓与突发事件瞬间暂停常规评分，启动防御协议。

---

## 四、 综合评分引擎数学模型

综合得分 (Composite Score, CS)：
$$CS = \sum_{i=1}^{4} W_i \times S_i \quad \in [-100, +100]$$

### 1. 维度权重配置
| 维度 | 长期仓位 (Long-term) | 短期仓位 (Short-term) | 核心定位 |
|------|:-------------------:|:--------------------:|----------|
| **消息面 (News)** | 35% | 15% | 长期宏观背景 vs 短期事件催化剂 |
| **数据面 (Data)** | 30% | 45% | 日内核心：杠杆清算机制与微观流动性 |
| **技术面 (Tech)** | 15% | 25% | 价格空间定位、风控止损与出场锚点 |
| **预测面 (Prediction)** | 20% | 15% | 市场预期差与赔率折算 |

### 2. 决策阈值阶梯 (多空不对称)
| 决策动作 (Action Decision) | CS 判定区间 | 策略行为说明 |
|--------------------------|:-----------:|--------------|
| **STRONG_LONG (强力做多)** | $CS \ge +60$ | 四面高共振，可上标准大仓位 |
| **STANDARD_LONG (标准做多)** | $+35 \le CS < +60$ | 多数维度看多，标准仓位进场 |
| **WATCH_LONG (偏多观望)** | $+10 \le CS < +35$ | 方向偏多但强度未饱和，等待二次确认 |
| **NEUTRAL (中性观望)** | $-10 < CS < +10$ | 市场无共识，空仓休息 |
| **WATCH_SHORT (偏空观望)** | $-40 < CS \le -10$ | 方向偏空但未达做空安全边际 |
| **STANDARD_SHORT (标准做空)** | $-65 < CS \le -40$ | 多数维度看空，标准仓位做空 |
| **STRONG_SHORT (强力做空)** | $CS \le -65$ | 极端过热或崩塌，强力顺势做空 |

---

## 五、 当前代码工程资产 (已落地并验证)

```text
crypto/
├── btc_factor_classification.md   # V7 理论手册
├── btc-four-face-monitor.html     # 四面监控面板 (可接实时 /api/live)
├── README.md / .cursorrules
├── config/weights.py + mapping.py # 权重、阈值、映射锚点
├── models/signals.py + snapshots.py
├── engine/scorer.py               # V7 双向评分引擎
├── collectors/
│   ├── binance_ws.py              # 大单 / 盘口±2% / 资金费率
│   ├── binance_klines.py          # OHLCV
│   ├── free_derivatives.py        # 免费 OI/LSR/爆仓 (Binance+Bybit+OKX)
│   ├── polymarket.py              # 预测面概率 + Fed 代理
│   ├── deribit.py                 # Max Pain + DVOL(IV)
│   ├── free_news.py               # ETF/DXY/减半/监管RSS
│   ├── free_onchain.py            # hashrate / 巨鲸 / USDT
│   └── coinglass.py               # 可选; 低档套餐会 Upgrade plan
├── indicators/                    # 结构/EMA/RSI/MACD/布林/ATR/VP/形态
├── mappers/
│   ├── data_mapper.py             # → S_data (含 Max Pain/IV/hashrate/whale)
│   ├── tech_mapper.py             # → S_tech
│   ├── news_mapper.py             # → S_news
│   └── prediction_mapper.py       # → S_prediction
├── runtime/
│   ├── live_loop.py               # 四面齐全 → 真 CS+安全阀; 缺面 → partial_CS
│   └── panel_server.py            # 兼容入口 → review.panel_server
├── review/
│   └── panel_server.py            # 完整面板: HTML + /api/live + 复盘 API
├── scripts/smoke_*.py / live_score.py
└── tests/                         # 勿用 discover 全跑 (含过期 V6)
```

**免费可跑主链路**：四面采集 → `S_news` / `S_data` / `S_tech` / `S_prediction` → 齐全时输出**完整 CS**（含安全阀）；缺面时回退 `partial_CS`（权重重归一化，不填 0 伪装）。

启动面板（推荐完整入口，含复盘档案 / 结算 / DeepSeek）：

```bash
cd crypto
python3 -m review.panel_server
# 浏览器打开 http://127.0.0.1:8787/
# 兼容: python3 -m runtime.panel_server （同样走完整 API）
```

烟雾脚本：

```bash
python3 scripts/smoke_news.py
python3 scripts/smoke_prediction.py
python3 scripts/live_score.py 60 20
```

### 已知限制（免费源诚实清单）

| 项 | 说明 |
|----|------|
| Predict.fun mainnet | 需 `PREDICT_FUN_API_KEY`；默认 **testnet** 打通管线（多为已结算盘，钉死价不计入打分） |
| CME FedWatch | 无免费 API → **Polymarket Fed 市场概率代替** |
| monetary_policy | 无点阵图 NLP → **DXY 5 日走势代理** |
| regulations | **RSS 关键词打分，低置信度** |
| black_swan / institutional_gov | 暂无独立自动化源 → missing + **权重重归一化兜底** |
| macro_data | **FRED CPI/利差** 已接入 |
| usdt_dynamics | **DefiLlama USDT 市值日变化** 已接入 |
| CVD | **Binance aggTrade 5min 滚动** 已接入 |
| liquidation_heatmap | 仍无稳定免费全网热力图 → 强平分布近似 / 归一化兜底 |
| MVRV / NUPL / SOPR 等 | 无可靠免费源（短期权重已为 0；长期可接 Bitview） |

**CS 公式 V2**：线性加权 × **一致性放大**（四面同向最多 ×1.3，对冲最低 ×0.7）。子指标缺项时 **renormalized_weighted_sum** 兜底。
| 链上 MVRV / NUPL / LTH / SOPR / 交易所存量 / 矿工持仓 | **无可靠免费源，本轮不做**；长期数据面 onchain 子层仍有大块空缺 |
| 清算热力图 | 多所强平价位近似，非 CoinGlass 原版 |
| Whale Alert / Farside | 免费层有频率限制或 HTML 抓取脆弱性；面板显示 `staleness` |
| 预测面子权重 | 手册未给，为工程默认值 (`PREDICTION_SUB_WEIGHTS`) |

---

## 六、 移交 Cursor 后的开发路线图 (Roadmap)

1. **Phase 1: 数据采集层** — ✅ 已完成 (CoinGlass 改为免费多所替代)
2. **Phase 2: 特征映射器** — ✅ 数据面 `S_data` 已完成
3. **Phase 3: 技术面指标** — ✅ `S_tech` 已完成 (纯 Python, 不依赖 ta-lib)
4. **Phase 4: 实时评分回路** — ✅ 四面齐全可出完整 CS；面板 `/api/live` 已接；黑天鹅为轻量标记（无解除状态机）
5. **Phase 5: 回测校准** — ⏳ `backtest/calibrator.py` 尚未落地
6. **Phase 6: 复盘自我改进** — ⏳ `review/` 骨架已有 (journal)，settle/DeepSeek/采纳闭环待做
