"""Binance USDⓈ-M 主网 API 凭据的只读验收。

安全边界
--------
这个模块的唯一用途是验证「主网 Futures API Key 能否进行签名读取」。
它刻意没有、也永远不应加入任何下单、撤单、设置杠杆、调整保证金、资金划转或
WebSocket 用户流功能。

允许的请求只有：
    GET /fapi/v1/time       （公共时间校准）
    GET /fapi/v2/account    （签名账户读取）

运行器主网执行仍由 ``trading.runtime_mode`` 明确阻断；本模块不改变该闸门。
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Dict, Tuple
from urllib.parse import urlencode

try:
    import aiohttp
except ImportError:  # pragma: no cover - 运行环境由项目依赖保证
    aiohttp = None

MAINNET_USDM_BASE = "https://fapi.binance.com"
READ_ONLY_PATHS = frozenset({"/fapi/v1/time", "/fapi/v2/account"})


class MainnetReadOnlyError(RuntimeError):
    """主网只读预检失败；错误消息不得包含凭据。"""


def _require_credential(value: str, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise MainnetReadOnlyError(f"{field} 不能为空")
    if len(result) > 512 or any(ch.isspace() for ch in result):
        raise MainnetReadOnlyError(f"{field} 格式不合法")
    return result


def signed_account_query(api_secret: str, server_time_ms: int,
                         recv_window: int = 5_000) -> Tuple[str, str]:
    """返回 GET /fapi/v2/account 的查询串与签名。

    这是纯函数，便于测试。调用方只能把它用于只读账户端点。
    """
    secret = _require_credential(api_secret, "API Secret")
    params = {
        "recvWindow": int(recv_window),
        "timestamp": int(server_time_ms),
    }
    query = urlencode(params)
    signature = hmac.new(
        secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return query, signature


def _safe_error(status: int, body: str) -> MainnetReadOnlyError:
    # 币安 body 只携带 code/msg；截断是为了避免代理页或异常页污染日志/前端。
    text = " ".join(str(body or "").split())[:300]
    return MainnetReadOnlyError(f"主网 Futures 只读预检失败 HTTP {status}: {text}")


async def verify_usdm_credentials_readonly(
    api_key: str,
    api_secret: str,
    *,
    timeout_sec: float = 15.0,
) -> Dict[str, Any]:
    """用两个 GET 请求验证主网 USDⓈ-M 凭据。

    成功只说明该凭据能够读取当前账户；**不代表**获准下单，也不修改任何
    交易所状态。任何失败均不回显 API Key/Secret。
    """
    if aiohttp is None:
        raise MainnetReadOnlyError("缺少 aiohttp，无法执行只读预检")
    key = _require_credential(api_key, "API Key")
    secret = _require_credential(api_secret, "API Secret")
    started = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=float(timeout_sec))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{MAINNET_USDM_BASE}/fapi/v1/time") as response:
                time_body = await response.text()
                if response.status != 200:
                    raise _safe_error(response.status, time_body)
                try:
                    server_time = int((await response.json()).get("serverTime") or 0)
                except Exception as exc:  # noqa: BLE001
                    raise MainnetReadOnlyError("主网时间接口返回格式异常") from exc
                if server_time <= 0:
                    raise MainnetReadOnlyError("主网时间接口未返回有效 serverTime")

            query, signature = signed_account_query(secret, server_time)
            url = f"{MAINNET_USDM_BASE}/fapi/v2/account?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": key}
            async with session.get(url, headers=headers) as response:
                account_body = await response.text()
                if response.status != 200:
                    raise _safe_error(response.status, account_body)
                try:
                    account = await response.json()
                except Exception as exc:  # noqa: BLE001
                    raise MainnetReadOnlyError("主网账户接口返回格式异常") from exc
    except MainnetReadOnlyError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MainnetReadOnlyError(
            f"主网 Futures 只读预检连接失败: {type(exc).__name__}"
        ) from exc

    return {
        "ok": True,
        "network": "mainnet",
        "base_url": MAINNET_USDM_BASE,
        "operation": "read_only_account_preflight",
        "http_methods": ["GET"],
        "execution_enabled": False,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "total_wallet_balance_usdt": float(account.get("totalWalletBalance") or 0),
        "available_balance_usdt": float(account.get("availableBalance") or 0),
        "can_trade": bool(account.get("canTrade")),
        "can_withdraw": bool(account.get("canWithdraw")),
    }
