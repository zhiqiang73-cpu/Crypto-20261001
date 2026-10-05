"""验证：系统会自动挂出止盈止损保护单（生产函数路径）。

用 shadow/deploy.py 的**生产函数**在真实账户上走一遍完整流程：
  账户准备（单向+逐仓+10x，与运行器启动一致）
  → 入场 0.002 BTC（分批止盈第 1 档为 50%，需 ≥0.002 才不被最小量过滤）
  → manage_exchange_stop() 自动挂止损（生产函数，并做交易所验证）
  → manage_take_profit() 自动挂止盈（生产函数，TP_STAGES 第 1 档）
  → 交易所核对两张保护单在场
  → 清理：撤保护单 + 平仓；终检仓位=0、挂单=0

结果写入 /tmp/protection_verify_result.json；任何异常都会尽力清理
（撤保护单 + 平仓）。

用法：
  bash scripts/run_protection_verify.sh   # 包装器（设置双重确认环境变量）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RESULT_PATH = "/tmp/protection_verify_result.json"
SYMBOL = "BTCUSDT"
QTY = 0.002  # ≈171 USDT；0.001 会被分批止盈的最小量（MIN_QTY=0.001/档）过滤
TAG = "pvf"

result = {
    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    "symbol": SYMBOL,
    "steps": [],
    "ok": False,
}


def step(name: str, **kw) -> None:
    entry = {"step": name, **kw}
    result["steps"].append(entry)
    print(f"[{name}] {json.dumps(kw, ensure_ascii=False, default=str)}", flush=True)


def save() -> None:
    try:
        with open(RESULT_PATH, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        print(f"[save_failed] {exc}", flush=True)


def fetch_atr_1h() -> float:
    """公共行情 1h K 线 → Wilder ATR(14)（与系统指标同口径）。"""
    import numpy as np
    from shadow.indicators import atr_wilder

    url = ("https://fapi.binance.com/fapi/v1/klines"
           "?symbol=BTCUSDT&interval=1h&limit=200")
    with urllib.request.urlopen(url, timeout=15) as resp:
        raw = json.loads(resp.read().decode())
    high = np.array([float(k[2]) for k in raw])
    low = np.array([float(k[3]) for k in raw])
    close = np.array([float(k[4]) for k in raw])
    return float(atr_wilder(high, low, close, 14)[-1])


async def main() -> int:
    # 0) 环境闸门
    from trading.runtime_mode import (
        current_mode, mainnet_confirmed, startup_status,
        validate_exchange_target,
    )
    step("gate", mode=current_mode().value,
         mainnet_confirmed=mainnet_confirmed(), startup=startup_status())
    try:
        validate_exchange_target("https://fapi.binance.com")
        step("gate_check", ok=True)
    except Exception as exc:  # noqa: BLE001
        step("gate_check", ok=False, error=str(exc))
        save()
        return 2

    # 1) 凭据
    from config.secrets import load_secrets
    secrets = load_secrets()
    key = (secrets.get("binance_mainnet_api_key") or "").strip()
    secret = (secrets.get("binance_mainnet_api_secret") or "").strip()
    if not key or not secret:
        step("credentials", ok=False, error="mainnet_credentials_missing")
        save()
        return 3
    step("credentials", ok=True, key_masked=key[:6] + "***" + key[-4:])

    from trading.binance_client import BinanceTestnetClient
    from trading import protective_orders as po
    from shadow import deploy as D

    client = BinanceTestnetClient(
        api_key=key, api_secret=secret, base_url="https://fapi.binance.com"
    )
    try:
        await client.sync_time(force=True)
        ping = await client.ping()
        bal = await client.get_balance()
        pos = await client.get_position(SYMBOL)
        mark = float(await client.mark_price(SYMBOL))
        try:
            min_notional = float(await client.min_notional(SYMBOL) or 0)
        except Exception:  # noqa: BLE001
            min_notional = 0.0
        pos_qty = float(getattr(pos, "quantity", 0) or 0)
        step("preflight", ping=bool(ping), mark=mark,
             min_notional=min_notional,
             available=float(getattr(bal, "available_balance", 0) or 0),
             position_qty=pos_qty,
             position_side=str(getattr(pos, "side", "")))
        if abs(pos_qty) > 1e-12:
            step("abort", reason="account_not_flat")
            save()
            return 4

        # 2) 账户准备：与运行器启动规格一致（单向 + 逐仓 + 10x）
        prep = {}
        for name, fn in (
            ("one_way", client.set_one_way_mode()),
            ("isolated", client.set_margin_type_isolated(SYMBOL)),
            ("leverage_10x", client.set_leverage(10, SYMBOL)),
        ):
            try:
                await fn
                prep[name] = "ok"
            except Exception as exc:  # noqa: BLE001
                prep[name] = f"{type(exc).__name__}: {exc}"
        step("account_prep", **prep)

        # 3) 入场（生产原语 place_limit_chase，与 sync_net 同用法）
        entry = await client.place_limit_chase(
            "LONG", QTY, symbol=SYMBOL, tag=TAG)
        filled = float(getattr(entry, "cum_filled_qty", 0) or 0)
        entry_px = float(getattr(entry, "avg_price", 0) or 0)
        step("entry", ok=bool(getattr(entry, "ok", False)), filled=filled,
             order_id=str(getattr(entry, "order_id", "")),
             entry_px=entry_px)
        if filled <= 0:
            step("abort", reason="entry_not_filled")
            save()
            return 5
        if entry_px <= 0:
            pos = await client.get_position(SYMBOL)
            entry_px = float(getattr(pos, "entry_price", 0) or 0)
        if entry_px <= 0:
            entry_px = mark

        # 4) 生产函数：自动止损 + 自动止盈
        atr_1h = fetch_atr_1h()
        st = D._default_state()
        st["strategies"] = {s.id: {} for s in D.specs_for_symbol(SYMBOL)}
        step("state", strategies=sorted(st["strategies"].keys()),
             atr_1h=round(atr_1h, 4), entry_px=entry_px)

        sl_info = await D.manage_exchange_stop(
            client, st, symbol=SYMBOL, ex_side=filled,
            entry_px=entry_px, atr_1h=atr_1h, execute=True)
        step("auto_stop_loss", info=sl_info,
             protection_state=D.protection_state(st, SYMBOL).get("state"))

        tp_info = await D.manage_take_profit(
            client, st, symbol=SYMBOL, ex_side=filled,
            entry_px=entry_px, atr_1h=atr_1h, execute=True)
        step("auto_take_profit", info=tp_info,
             tp_orders=len(D.protection_state(st, SYMBOL).get("tp_orders")
                           or []))

        # 5) 交易所核对
        orders = await po.fetch_open_protective_orders(client, SYMBOL)
        is_tp = getattr(
            po, "is_take_profit_type",
            lambda o: "TAKE_PROFIT" in str(
                o.get("orderType") or o.get("type") or "").upper())
        alive = getattr(po, "algo_alive", lambda o: True)
        summary = []
        for o in orders or []:
            summary.append({
                "algoId": str(o.get("algoId") or o.get("orderId") or ""),
                "type": str(o.get("orderType") or o.get("type") or ""),
                "side": str(o.get("side") or ""),
                "trigger": o.get("triggerPrice") or o.get("stopPrice")
                or o.get("price"),
                "qty": o.get("quantity") or o.get("origQty"),
                "reduceOnly": o.get("reduceOnly"),
                "is_tp": bool(is_tp(o)),
                "alive": bool(alive(o)),
            })
        step("exchange_orders", count=len(summary), orders=summary)
        has_sl = any((not s["is_tp"]) and s["alive"] for s in summary)
        has_tp = any(s["is_tp"] and s["alive"] for s in summary)
        step("verdict", auto_stop_loss=bool(sl_info),
             auto_take_profit=bool(tp_info),
             exchange_has_stop=has_sl, exchange_has_tp=has_tp)

        # 6) 清理：撤保护单 + 平仓
        cancelled, remain = await po.cancel_protective_orders(
            client, SYMBOL, include_take_profit=True)
        step("cleanup_cancel", cancelled=cancelled, remaining=remain)
        close = await client.place_limit_chase(
            "SHORT", filled, symbol=SYMBOL, reduce_only=True, tag=TAG)
        step("close", ok=bool(getattr(close, "ok", False)),
             filled=float(getattr(close, "cum_filled_qty", 0) or 0))
        pos2 = await client.get_position(SYMBOL)
        q2 = float(getattr(pos2, "quantity", 0) or 0)
        orders2 = await client.get_open_orders(SYMBOL)
        step("final", position_qty=q2, open_orders=len(orders2 or []))
        result["ok"] = (abs(q2) <= 1e-12) and has_sl and has_tp
        save()
        return 0 if result["ok"] else 6
    except Exception as exc:  # noqa: BLE001
        step("exception", error=f"{type(exc).__name__}: {exc}",
             trace=traceback.format_exc()[-1500:])
        try:
            await po.cancel_protective_orders(
                client, SYMBOL, include_take_profit=True)
            step("cleanup_cancel_stops", ok=True)
        except Exception as e2:  # noqa: BLE001
            step("cleanup_cancel_stops", ok=False, error=str(e2))
        try:
            pos3 = await client.get_position(SYMBOL)
            q3 = float(getattr(pos3, "quantity", 0) or 0)
            if abs(q3) > 1e-12:
                side3 = "SHORT" if q3 > 0 else "LONG"
                await client.place_limit_chase(
                    side3, abs(q3), symbol=SYMBOL, reduce_only=True, tag=TAG)
                step("cleanup_close", ok=True, qty=abs(q3))
            else:
                step("cleanup_close", ok=True, qty=0)
        except Exception as e3:  # noqa: BLE001
            step("cleanup_close", ok=False, error=str(e3))
        save()
        return 7
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
