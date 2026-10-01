"""成交费用/资金费适配器 — 官方结构样本解析 + 假交易所接入.

真实账户未授权时用固定 JSON 样本验证；未知费用不得伪装为 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FeeFill:
    exchange_trade_id: str
    order_id: str
    client_order_id: str = ""
    symbol: str = ""
    qty: float = 0.0
    price: float = 0.0
    commission: Optional[float] = None
    commission_asset: Optional[str] = None
    realized_pnl: Optional[float] = None
    is_maker: Optional[bool] = None
    fee_unknown: bool = True
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IncomeRecord:
    income_type: str  # COMMISSION / REALIZED_PNL / FUNDING_FEE
    income: float
    asset: str = "USDT"
    tran_id: str = ""
    trade_id: str = ""
    symbol: str = ""
    time_ms: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


def parse_user_trade(row: dict) -> FeeFill:
    """解析 Binance futures userTrades 单行（官方字段）."""
    commission = row.get("commission")
    asset = row.get("commissionAsset")
    known = commission is not None
    return FeeFill(
        exchange_trade_id=str(row.get("id") or row.get("tradeId") or ""),
        order_id=str(row.get("orderId") or ""),
        client_order_id=str(row.get("clientOrderId") or row.get("buyerOrderId") or ""),
        symbol=str(row.get("symbol") or ""),
        qty=float(row.get("qty") or row.get("quantity") or 0),
        price=float(row.get("price") or 0),
        commission=float(commission) if known else None,
        commission_asset=str(asset) if asset is not None else None,
        realized_pnl=float(row["realizedPnl"]) if row.get("realizedPnl") is not None else None,
        is_maker=bool(row["maker"]) if "maker" in row else None,
        fee_unknown=not known,
        raw=dict(row),
    )


def parse_income(row: dict) -> IncomeRecord:
    return IncomeRecord(
        income_type=str(row.get("incomeType") or ""),
        income=float(row.get("income") or 0),
        asset=str(row.get("asset") or "USDT"),
        tran_id=str(row.get("tranId") or ""),
        trade_id=str(row.get("tradeId") or ""),
        symbol=str(row.get("symbol") or ""),
        time_ms=int(row.get("time") or 0),
        raw=dict(row),
    )


# 官方结构固定样本（不连账户）
SAMPLE_USER_TRADE = {
    "symbol": "BTCUSDT",
    "id": 987654321,
    "orderId": 123456,
    "side": "SELL",
    "price": "95000.10",
    "qty": "0.002",
    "realizedPnl": "-1.25",
    "quoteQty": "190.0002",
    "commission": "0.076",
    "commissionAsset": "USDT",
    "time": 1700000000000,
    "buyer": False,
    "maker": False,
    "clientOrderId": "cid_demo_1",
}

SAMPLE_USER_TRADE_NO_FEE = {
    "symbol": "BTCUSDT",
    "id": 987654322,
    "orderId": 123457,
    "price": "95000.00",
    "qty": "0.001",
    "time": 1700000001000,
    "clientOrderId": "cid_demo_2",
}

SAMPLE_INCOME_FUNDING = {
    "symbol": "BTCUSDT",
    "incomeType": "FUNDING_FEE",
    "income": "-0.42",
    "asset": "USDT",
    "time": 1700003600000,
    "tranId": 555,
    "tradeId": "",
}


class FeeAdapter:
    """从客户端拉取成交/收入并转为可记账结构."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._processed_trade_ids: set = set()

    def parse_trades(self, rows: List[dict]) -> List[FeeFill]:
        out = []
        for row in rows or []:
            fill = parse_user_trade(row)
            if fill.exchange_trade_id and fill.exchange_trade_id in self._processed_trade_ids:
                continue
            if fill.exchange_trade_id:
                self._processed_trade_ids.add(fill.exchange_trade_id)
            out.append(fill)
        return out

    async def fetch_trades(self, symbol: str, **kwargs) -> List[FeeFill]:
        if hasattr(self.client, "user_trades"):
            rows = await self.client.user_trades(symbol=symbol, **kwargs)
            return self.parse_trades(rows)
        if hasattr(self.client, "get_user_trades"):
            rows = await self.client.get_user_trades(symbol=symbol, **kwargs)
            return self.parse_trades(rows)
        return []

    async def fetch_income(self, **kwargs) -> List[IncomeRecord]:
        if hasattr(self.client, "get_income"):
            rows = await self.client.get_income(**kwargs)
            return [parse_income(r) for r in (rows or [])]
        return []
