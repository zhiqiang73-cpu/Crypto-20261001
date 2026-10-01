# 回测与策略验证

这四个脚本是 2026-10-01 那轮「从图上找赚钱策略」的系统性检验工具，
结论见 [`docs/BTCUSDT_策略验证报告.md`](../../docs/BTCUSDT_策略验证报告.md)。

## 前置数据

脚本读取 `inputs/klines/BTCUSDT_{1d,4h,1h}.csv`（该目录被 gitignore，不进仓库）。
可用仓库内的采集器重新拉取：

```bash
python3 -c "from collectors.binance_klines import ..."   # 或直接调用现成的拉取脚本
```

CSV 列为币安原生格式：`open_time,open,high,low,close,volume,close_time,...`。

## 四个脚本

| 脚本 | 作用 |
| --- | --- |
| `edge_scan.py <tf>` | 21 个策略 × 1 个周期的全家族扫描，输出样本内 / 样本外对照 |
| `deep_dive.py` | 成交量冲击策略的 192 组参数网格 + 扩窗走步验证（4h / 1h） |
| `robust_check.py` | 最小参数策略跨周期对照（判断信号是真信号还是行情巧合） |
| `trend_filter_test.py` | 趋势过滤多头策略的参数稳健性 + **逐根盯市**回撤 |

```bash
python3 scripts/backtest/edge_scan.py 4h
python3 scripts/backtest/deep_dive.py
python3 scripts/backtest/robust_check.py
python3 scripts/backtest/trend_filter_test.py
```

## 方法学约定（改脚本时不要破坏）

1. **无未来函数** — 第 i 根收盘产生信号，第 i+1 根**开盘价**成交。
2. **扣真实成本** — `COST_PER_SIDE = 0.0005 + 0.0002`（taker + 滑点），每换向付两次。
3. **样本内 / 样本外** — 前 70% 挑参数，后 30% 只检验。只报样本内等于自欺。
4. **走步验证** — 单次分割不算验证，必须扩窗多折。
5. **逐根盯市回撤** — 只在平仓时更新净值会严重低估回撤（`trend_filter_test.py` 是正确写法）。

## 已知局限

- 未计入**资金费率**。多头长期持有需付费，会让「只做多」的优势略微缩小。
- 样本外区间（2024-2026）仍是上涨行情，熊市压力测试样本有限。
- 单一标的（BTCUSDT）。ETHUSDT 数据在本地，可直接复现检验。
