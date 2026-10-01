# BTC Quant Console 新前端

这是全新重做的前端，不复用旧 V7 面板的页面结构和样式。

- 入口目录：`frontend/`
- 预览端口：`8788`
- 旧后端 API：`8787`
- 默认安全状态：Paper Mode
- 核心页面：交易总览、策略中心、仓位与订单、交易日志、风控闸门
- 新前端通过 API 读取行情和运行状态；API 不可用时显示明确的安全占位状态，不伪造成交。

启动：

```bash
sh scripts/start_new_frontend.sh
```
