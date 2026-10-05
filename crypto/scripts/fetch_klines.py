"""把 Binance USDT-M 永续的公开 K 线下载到 inputs/klines/。

只读公开行情接口（fapi/v1/klines），**不涉及任何下单、不读密钥、不连账户**。
用于给回测/标定提供本地历史数据。

用法：
    python3 -m scripts.fetch_klines --symbol BTCUSDT --interval 5m \
        --start 2021-01-01 --out inputs/klines
    python3 -m scripts.fetch_klines --symbol ETHUSDT --interval 1h --start 2021-01-01

输出 CSV 列与既有 inputs/klines/*.csv 完全一致：
    open_time,open,high,low,close,volume,close_time,quote_volume,trades,taker_base,taker_quote,ignore
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

BASE = "https://fapi.binance.com/fapi/v1/klines"
LIMIT = 1500                      # 单次请求上限
HEADER = ["open_time", "open", "high", "low", "close", "volume", "close_time",
          "quote_volume", "trades", "taker_base", "taker_quote", "ignore"]


def _ms(text: str) -> int:
    dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fetch_page(symbol: str, interval: str, start_ms: int, retries: int = 4) -> list:
    url = (f"{BASE}?symbol={symbol}&interval={interval}"
           f"&startTime={start_ms}&limit={LIMIT}")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "crypto-backtest/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            # 429/418 = 触发限频，退避后重试
            wait = 2 ** attempt * (5 if exc.code in (429, 418) else 1)
            print(f"    HTTP {exc.code}，等待 {wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
        except Exception as exc:                       # noqa: BLE001
            print(f"    网络错误 {exc!r}，等待 {2 ** attempt}s 后重试", file=sys.stderr)
            time.sleep(2 ** attempt)
    raise RuntimeError(f"连续 {retries} 次请求失败: {symbol} {interval} @ {start_ms}")


def download(symbol: str, interval: str, start_ms: int, out_path: str) -> int:
    rows: list = []
    cursor = start_ms
    while True:
        page = fetch_page(symbol, interval, cursor)
        if not page:
            break
        rows.extend(page)
        last_open = int(page[-1][0])
        if len(page) < LIMIT:
            break
        cursor = last_open + 1
        if len(rows) % 30000 < LIMIT:
            print(f"    已下载 {len(rows):,} 根 … 最新 {datetime.fromtimestamp(last_open/1000, tz=timezone.utc):%Y-%m-%d %H:%M}")
        time.sleep(0.12)                               # 温和限速，避免触发风控
    # 去重 + 排序
    seen = {}
    for r in rows:
        seen[int(r[0])] = r
    ordered = [seen[k] for k in sorted(seen)]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER)
        for r in ordered:
            w.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6],
                        r[7], r[8], r[9], r[10], r[11] if len(r) > 11 else 0])
    return len(ordered)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--interval", required=True)
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--out", default="inputs/klines")
    args = ap.parse_args()

    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    out_path = os.path.join(root, args.out, f"{args.symbol.upper()}_{args.interval}.csv")
    start_ms = _ms(args.start)
    print(f"[下载] {args.symbol} {args.interval}  起点 {args.start} → {out_path}")
    n = download(args.symbol.upper(), args.interval, start_ms, out_path)
    print(f"[完成] {n:,} 根 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
