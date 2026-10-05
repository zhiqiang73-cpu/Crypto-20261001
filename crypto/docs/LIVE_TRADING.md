# 实盘（主网真实资金）操作手册

本仓库 = 实盘系统（含止盈止损 + 主网双重确认 + 主网凭据接线）。
目录: `/Users/zengyun/我的AI/crypto/.work/rt-dev/crypto`

## 一、保存主网密钥（一次性）
1. 运行 `scripts/start_live_panel.sh`
2. 打开面板 http://127.0.0.1:8787/ → 账户页 → 「主网 API 凭据部署」
3. 填 Key / Secret → 保存 → 点「测试」（只读校验：查时间 + 账户，不下任何单）

## 二、启动实盘
1. 运行 `scripts/start_live_runner.sh`，输入 `yes`
2. 看到「主网真实资金模式已启用」横幅 = 已开始实盘交易
3. 停止：Ctrl-C

## 三、安全机制（代码强制，勿绕过）
- 双重确认缺一不可: `TRADING_MODE=live` + `CONFIRM_MAINNET=YES_I_UNDERSTAND`
- 行情腿与下单腿必须同市场（启动闸门强制校验）
- 保护单以交易所侧 STOP_MARKET 为主，进程内止损兜底；保护单未确认时禁止开新仓
- 未保存密钥 / 未确认 → 拒绝启动
- 默认（不加 live 环境变量）行为与旧版完全一致：测试网

## 四、建议：先只读观察一次

```bash
TRADING_MODE=live CONFIRM_MAINNET=YES_I_UNDERSTAND python3 -m shadow.deploy --interval 15
```

去掉 `--execute` = 只看行情与账户、不下单；确认一切正常后再用脚本实盘。
