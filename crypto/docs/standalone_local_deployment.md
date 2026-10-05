# 独立本地部署

这套系统按“自己的电脑/服务器上独立运行”设计，不依赖云端托管。

## 运行结构

```text
本机浏览器
  └─ frontend/:8788  新控制台
       └─ review.panel_server:8787  本地 API、账户同步、策略运行和账本
            └─ Binance Futures Testnet REST/WebSocket
```

## 启动

```bash
cd "/Users/zengyun/我的AI/crypto"
python3 -m pip install -r requirements.txt
TRADING_MODE=paper sh scripts/run_local_console.sh
```

浏览器打开 `http://127.0.0.1:8788/`。

- 默认 `paper`，不发送订单。
- Testnet 账户连接从“账户连接”页面输入，密钥只发送到本机后端并保存到本地运行时文件。
- 不要把 8787/8788 暴露到公网；要 24 小时运行时建议使用 systemd、launchd、Docker Compose 或 supervisor。
- 主网模式当前被硬闸门阻断。
