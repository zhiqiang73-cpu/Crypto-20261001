# 移交 Cursor 的开箱即用 Prompt (可直接复制至 Cursor Composer / Chat)

```markdown
你好，Cursor！你现在接手开发一个专门针对 BTC/USDT 的量化交易系统。
项目仓库目录已经完整准备就绪，请你阅读并以项目根目录下的两个核心文档为纲要开始工作：
1. `README.md`：项目的总体背景、双仓位策略、数学模型、工程架构与开发路线图。
2. `btc_factor_classification.md`：核心理论手册 (V7 双向统一评分版)，详述了影响 BTC/USDT 的四面一体指标规范、量化打分阈值与双向决策因果链。
3. `.cursorrules`：你的工程行为准则和量化约束。

---

### 一、 核心量化框架概述
- **四面一体**：消息面 (News)、数据面 (Data)、技术面 (Tech)、预测面 (Prediction)。
- **双仓位机制**：
  - 长期核心仓位 (月级别): 消息 35% + 数据 30% + 预测 20% + 技术 15%
  - 短期战术仓位 (日内): 数据 45% (核心) + 技术 25% + 消息 15% + 预测 15%
- **统一双向评分量表**：$S_i \in [-100, +100]$，正值看多，负值看空，0 为中性。
  - 综合得分：$CS = \sum W_i \times S_i \in [-100, +100]$
- **多空不对称阈值**：
  - 强力做多: $CS \ge +60$ | 标准做多: $+35 \le CS < +60$ | 偏多观望: $+10 \le CS < +35$
  - 中性空仓: $-10 < CS < +10$
  - 偏空观望: $-40 < CS \le -10$ | 标准做空: $-65 < CS \le -40$ | 强力做空: $CS \le -65$
- **安全阀机制**：任一维度 $S_i$ 与 $CS$ 符号相反且差值 $> 50$，自动降级决策。

---

### 二、 已就绪的工程代码基线
项目目前已有以下 Python 3.9+ 核心模块，并通过了 8 项严密单元测试：
- `config/weights.py`：所有长短期权重、子层内部权重、多空阈值与安全阀配置。
- `models/signals.py`：DimensionScores, ActionDecision, EvaluationResult 等强类型数据类。
- `engine/scorer.py`：V7 双向统一加权综合评分引擎 `FactorScoringEngine`。
- `backtest/calibrator.py`：基于网格搜索的入场阈值与期望值校准器。
- `tests/test_scorer_v7.py`：单元测试集（可通过 `python3 -m unittest tests/test_scorer_v7.py` 运行验证）。

---

### 三、 你的接手首要任务 (Phase 1: 数据采集层)
请按照系统设计路线图，开始着手开发数据采集模块 `collectors/`：
1. **Binance WebSocket 采集器 (`collectors/binance_ws.py`)**：
   - 接入 Binance Futures WebSocket（行情源 `wss://fstream.binance.com/ws`）。
   - 订阅并实时消费：
     - `btcusdt@aggTrade`：提取单笔成交额大于 50 万美元的买卖盘大单。
     - `btcusdt@depth20@100ms`：提取买卖盘深度比率 (Bid/Ask Depth Ratio ±2%) 与盘口 Spread。
     - `btcusdt@markPrice@1s`：提取实时资金费率 (Funding Rate) 与标记价格。
2. **CoinGlass API 采集器 (`collectors/coinglass.py`)**：
   - 获取全网 BTC 合约持仓量 (Open Interest) 变动。
   - 获取多空爆仓与清算热力图集中区间。
3. **编写对应的映射器原型 (`mappers/data_mapper.py`)**：
   - 将上述采集到的资金费率、订单簿比例、OI 变动等原始数据，根据 `btc_factor_classification.md` 第五节定义的规则，自动转换为 $[-100, +100]$ 的子分值。

请你先检查现有项目结构与测试，确认完全理解后，立即开始构建 `collectors/` 模块。
```
