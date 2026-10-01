"""密钥管理 — 环境变量优先, runtime/secrets.json 回退, 不入库.

优先级: 环境变量 > secrets.json > 空
面板写入时同步落盘到 secrets.json (gitignore)。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from config.review import PROJECT_ROOT

logger = logging.getLogger(__name__)

SECRETS_FILE = PROJECT_ROOT / "runtime" / "secrets.json"

# 允许的字段白名单 — 禁止往 secrets.json 写任意键
ALLOWED_KEYS = frozenset({
    "deepseek_api_key",
    "deepseek_model",
    "deepseek_base_url",
    "binance_testnet_api_key",
    "binance_testnet_api_secret",
    "binance_testnet_base_url",
    "predict_fun_api_key",
    "cryptopanic_api_key",
})

ENV_MAP = {
    "deepseek_api_key": "DEEPSEEK_API_KEY",
    "deepseek_model": "DEEPSEEK_MODEL",
    "deepseek_base_url": "DEEPSEEK_BASE_URL",
    "binance_testnet_api_key": "BINANCE_TESTNET_API_KEY",
    "binance_testnet_api_secret": "BINANCE_TESTNET_API_SECRET",
    "binance_testnet_base_url": "BINANCE_TESTNET_BASE_URL",
    "predict_fun_api_key": "PREDICT_FUN_API_KEY",
    "cryptopanic_api_key": "CRYPTOPANIC_API_KEY",
}


def _read_file() -> Dict[str, Any]:
    if not SECRETS_FILE.exists():
        return {}
    try:
        data = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if k in ALLOWED_KEYS and v}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("secrets: 读取失败 %s: %s", SECRETS_FILE, exc)
        return {}


def load_secrets() -> Dict[str, str]:
    """合并环境变量与 secrets.json. 环境变量覆盖文件."""
    out: Dict[str, str] = {}
    file_data = _read_file()
    for key in ALLOWED_KEYS:
        env_name = ENV_MAP.get(key)
        env_val = (os.environ.get(env_name) or "").strip() if env_name else ""
        if env_val:
            out[key] = env_val
        elif file_data.get(key):
            out[key] = str(file_data[key]).strip()
    return out


def save_secrets(updates: Dict[str, Any], merge: bool = True) -> Dict[str, str]:
    """写入 secrets.json. merge=True 时与现有合并."""
    current = _read_file() if merge else {}
    for k, v in updates.items():
        if k not in ALLOWED_KEYS:
            continue
        if v is None or (isinstance(v, str) and not v.strip()):
            current.pop(k, None)
        else:
            current[k] = str(v).strip()
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SECRETS_FILE.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(SECRETS_FILE, 0o600)
    except OSError:
        pass
    return load_secrets()


def get_secret(key: str, default: Optional[str] = None) -> Optional[str]:
    return load_secrets().get(key, default)


def mask_secret(value: Optional[str]) -> str:
    if not value:
        return "(未配置)"
    k = value.strip()
    if len(k) <= 8:
        return "****"
    return f"{k[:4]}****{k[-4:]}"
