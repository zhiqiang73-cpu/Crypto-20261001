"""主网最小单位功能测试（真实资金，受控一次性）。

流程（每一步都写入结果 JSON）：
  0. 环境闸门自检（TRADING_MODE=live + CONFIRM_MAINNET=YES_I_UNDERSTAND）
  1. 凭据预检（runtime/secrets.json 中的主网 Key，不打印明文）
  2. 只读预检：时间同步 / ping / 余额 / 持仓（必须空仓）/ 标记价 / 最小下单量
  3. 限价追价入场（最小可下单量，tag=mnt）
  4. 挂保护单：止损 STOP_MARKET（显式数量 + reduceOnly）+ 止盈 LIMIT reduceOnly
     并验证两张单在交易所可见（protects == True）
  5. 撤掉两张保护单
  6. 限价追价平仓（reduceOnly）
  7. 终检：仓位为空、无挂单

任何异常：尽力清理（撤保护单 + 平掉残余仓位），结果写入
/tmp/mainnet_smoke_result.json。

用法：
  DRY_RUN=1 python3 scripts/mainnet_min_smoke.py   # 只做 0-2 步与数量计划，不下任何单
  python3 scripts/mainnet_min_smoke.py             # 真实执行（需双确认环境变量，建议用
                                                   #   scripts/run_mainnet_smoke.sh 包装器）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RESULT_PATH = "/tmp/mainnet_smoke_result.json"
SYMBOL = "BTCUSDT"
# BTCUSDT 交易所过滤器最小名义金额为 50 USDT（由 min_notional() 实时读取），
# 0.001 BTC（≈85 USDT）即满足；再往上由 min_notional/价格 动态决定。
MIN_QTY = 0.001

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


async def main() -> int:
    dry = os.environ.get("DRY_RUN") == "1"
    result["dry_run"] = dry

    # 0) 环境闸门
    from trading.runtime_mode import (
        current_mode,
        mainnet_confirmed,
        startup_status,
        validate_exchange_target,
    )

    step(
        "gate",
        mode=current_mode().value,
        mainnet_confirmed=mainnet_confirmed(),
        startup=startup_status(),
    )
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
        print("MISSING_CREDENTIALS", flush=True)
        return 3
    step("credentials", ok=True, key_masked=key[:6] + "***" + key[-4:])

    # 2) 客户端 + 只读预检
    from trading.binance_client import BinanceTestnetClient

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
        step(
            "preflight",
            ping=bool(ping),
            mark=mark,
            min_notional=min_notional,
            available=float(getattr(bal, "available_balance", 0) or 0),
            position_qty=pos_qty,
            position_side=str(getattr(pos, "side", "")),
        )
        if abs(pos_qty) > 1e-12:
            step("abort", reason="account_not_flat")
            save()
            return 4
        need = max(MIN_QTY, (min_notional / mark) if mark else MIN_QTY)
        qty = round(need + 0.0004, 3)  # 向上取到 0.001 步长
        step("plan", qty=qty, est_notional=round(qty * mark, 2))
        if dry:
            step("dry_run_done", note="未下任何单")
            save()
            return 0

        # 3) 入场
        entry = await client.place_limit_chase("LONG", qty, symbol=SYMBOL, tag="mnt")
        filled = float(getattr(entry, "cum_filled_qty", 0) or 0)
        step(
            "entry",
            ok=bool(getattr(entry, "ok", False)),
            filled=filled,
            order_id=str(getattr(entry, "order_id", "")),
        )
        if filled <= 0:
            step("abort", reason="entry_not_filled")
            save()
            return 5

        # 4) 保护单（止损 + 止盈）
        from trading import protective_orders as po

        sl_trigger = round(mark * 0.995, 1)
        tp_trigger = round(mark * 1.005, 1)
        sl = await po.place_protective_stop(
            client,
            symbol=SYMBOL,
            side=1,
            trigger_price=sl_trigger,
            quantity=filled,
            close_position=False,
        )
        step(
            "stop_loss",
            protects=sl.protects,
            algo_id=sl.algo_id,
            trigger=sl.trigger_price,
            error=sl.error,
        )
        tp = await po.place_take_profit(
            client, symbol=SYMBOL, side=1, trigger_price=tp_trigger, quantity=filled
        )
        step(
            "take_profit",
            protects=tp.protects,
            algo_id=tp.algo_id,
            trigger=tp.trigger_price,
            error=tp.error,
        )

        # 5) 撤保护单
        for name, order in (("stop_loss", sl), ("take_profit", tp)):
            if order.algo_id:
                try:
                    ok = await po.cancel_algo_by_id(client, SYMBOL, order.algo_id)
                except Exception as exc:  # noqa: BLE001
                    ok = False
                    step(f"cancel_{name}", ok=False, error=str(exc))
                    continue
                step(f"cancel_{name}", ok=bool(ok))

        # 6) 平仓
        close = await client.place_limit_chase(
            "SHORT", filled, symbol=SYMBOL, reduce_only=True, tag="mnt"
        )
        step(
            "close",
            ok=bool(getattr(close, "ok", False)),
            filled=float(getattr(close, "cum_filled_qty", 0) or 0),
        )

        # 7) 终检
        pos2 = await client.get_position(SYMBOL)
        q2 = float(getattr(pos2, "quantity", 0) or 0)
        orders = await client.get_open_orders(SYMBOL)
        step("final", position_qty=q2, open_orders=len(orders or []))
        result["ok"] = abs(q2) <= 1e-12
        save()
        return 0 if result["ok"] else 6
    except Exception as exc:  # noqa: BLE001
        step(
            "exception",
            error=f"{type(exc).__name__}: {exc}",
            trace=traceback.format_exc()[-1500:],
        )
        # 尽力清理：撤保护单 + 平仓
        try:
            await client.cancel_all_stops(SYMBOL)
            step("cleanup_cancel_stops", ok=True)
        except Exception as e2:  # noqa: BLE001
            step("cleanup_cancel_stops", ok=False, error=str(e2))
        try:
            pos3 = await client.get_position(SYMBOL)
            q3 = float(getattr(pos3, "quantity", 0) or 0)
            if abs(q3) > 1e-12:
                side3 = "SHORT" if q3 > 0 else "LONG"
                await client.place_limit_chase(
                    side3, abs(q3), symbol=SYMBOL, reduce_only=True, tag="mnt"
                )
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
    code = asyncio.run(main())
    print("EXIT", code, flush=True)
    sys.exit(code)
