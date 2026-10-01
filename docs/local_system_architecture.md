# BTCUSDT 本地量化系统框架

## 当前交付边界

- **运行位置**：纯本地/自有服务器；面板默认只监听 `127.0.0.1:8787`。
- **交易标的**：Binance USDⓈ-M Futures，`BTCUSDT`。
- **当前安全模式**：`paper`（默认）或 Binance Futures Testnet；不允许主网 URL，不开放 LIVE 模式。
- **密钥**：只从环境变量或 `runtime/secrets.json` 读取；不要把 key/secret 写进代码、Git 或聊天。
- **AI 自我改进**：只生成受白名单和幅度护栏约束的配置建议；人工采纳后才生成新版本，旧版本可回滚。

## 组件

1. `collectors/`：行情、资金费率、衍生品、新闻和预测源。
2. `indicators/` + `mappers/`：技术特征和四面因子映射。
3. `engine/`：[-100,100] 评分、阈值和安全阀。
4. `trading/`：Binance Testnet 客户端、订单幂等、仓位账本、独立 RiskGuardian、止盈止损。
5. `review/`：成交日志、结算、统计、DeepSeek 建议和配置版本。
6. `config/strategy_registry.py`：Claude 结论的声明式策略接入；不接受任意代码。
7. `review/panel_server.py`：本地 HTTP API + 原有监控面板。

## Claude 策略输入格式

将验证后的结论整理成 JSON，提交到策略注册表（后续前端 API 会调用同一校验器）：

```json
{
  "strategy_id": "btc-mean-reversion",
  "name": "BTC 均值回归",
  "kind": "mean_reversion",
  "timeframe": "15m",
  "side": "both",
  "entry": {"indicator": "bbands", "zscore": -2.0},
  "exit": {"take_profit_r": 1.5, "stop_loss_r": 1.0},
  "risk": {"max_risk_pct": 0.005, "leverage": 2},
  "source_note": "验证结论、样本区间、失效条件"
}
```

策略写入后默认 `paper_only` 且 `enabled=false`。进入 Testnet 前，应补充回测报告、样本外结果、手续费/滑点假设、最大回撤、失效条件和人工验收记录。

## 启动

```bash
python3 -m pip install -r requirements.txt
TRADING_MODE=paper python3 -m review.panel_server
# 浏览器访问 http://127.0.0.1:8787/
```

24 小时运行时使用你自己的服务器进程管理器（systemd、Docker Compose 或 supervisor），并保留本地磁盘备份；不要直接将 8787 暴露到公网。

## 上线前硬性检查

- Testnet 账号和 API key 仅开启合约交易，关闭提现；能限制 IP 时必须限制。
- 默认 `trading_enabled=false`；先验证余额、持仓、订单查询和保护单对账。
- 断网、时钟漂移、行情陈旧、保护单失败、对账不一致时必须停止开仓，只允许减仓/保护。
- 任何主网切换都应当是独立人工审查后的新版本，不通过聊天直接配置。
