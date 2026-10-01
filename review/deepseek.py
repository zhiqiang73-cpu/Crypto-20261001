"""DeepSeek 客户端 (OpenAI 兼容 /chat/completions).

安全约定:
  * API key 只作为参数传入, 本模块不落盘、不打日志、不回显。
  * 出错信息里只出现掩码后的 key (sk-****abcd), 便于排查又不会泄漏。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore

from config.review import (
    DEEPSEEK_DEFAULT_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_MAX_TOKENS,
    DEEPSEEK_TEMPERATURE,
    DEEPSEEK_TIMEOUT_SEC,
    DEEPSEEK_VERIFY_MAX_TOKENS,
)

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class DeepSeekError(RuntimeError):
    """DeepSeek 调用失败的基类."""


class DeepSeekAuthError(DeepSeekError):
    """key 无效或没有权限."""


class DeepSeekRateLimitError(DeepSeekError):
    """限流 / 余额不足."""


def mask_key(key: Optional[str]) -> str:
    if not key:
        return "(未配置)"
    k = key.strip()
    if len(k) <= 8:
        return "****"
    return f"{k[:5]}****{k[-4:]}"


def parse_json_content(content: str) -> Any:
    """从模型输出里抠出 JSON. 容忍 ```json 围栏与前后废话."""
    if not content:
        raise DeepSeekError("模型返回空内容")
    text = content.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise DeepSeekError(f"无法解析模型返回的 JSON: {exc}") from exc
    raise DeepSeekError("模型返回里没有 JSON 对象")


class DeepSeekClient:
    """最小可用的异步客户端."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEEPSEEK_DEFAULT_BASE_URL,
        model: str = DEEPSEEK_DEFAULT_MODEL,
        timeout_sec: float = DEEPSEEK_TIMEOUT_SEC,
    ) -> None:
        if aiohttp is None:
            raise DeepSeekError("需要 aiohttp 才能调用 DeepSeek")
        self.api_key = (api_key or "").strip()
        self.base_url = (base_url or DEEPSEEK_DEFAULT_BASE_URL).rstrip("/")
        self.model = model or DEEPSEEK_DEFAULT_MODEL
        self.timeout_sec = timeout_sec
        self._session: Optional[Any] = None

    # ------------------------------------------------------------------ 基础设施
    async def _ensure_session(self) -> Any:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout_sec)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            raise DeepSeekAuthError("未配置 API key")
        session = await self._ensure_session()
        url = f"{self.base_url}{path}"
        try:
            async with session.post(url, json=body, headers=self.headers) as resp:
                text = await resp.text()
                if resp.status == 401:
                    raise DeepSeekAuthError(
                        f"API key 无效 ({mask_key(self.api_key)}), 请到面板「配置」页重新填写"
                    )
                if resp.status == 402:
                    raise DeepSeekRateLimitError("DeepSeek 账户余额不足")
                if resp.status == 429:
                    raise DeepSeekRateLimitError("DeepSeek 限流, 稍后重试")
                if resp.status >= 400:
                    raise DeepSeekError(f"DeepSeek 返回 {resp.status}: {text[:300]}")
                return json.loads(text)
        except aiohttp.ClientError as exc:
            raise DeepSeekError(f"连接 DeepSeek 失败: {exc}") from exc

    # ------------------------------------------------------------------ 对话
    async def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
        model: Optional[str] = None,
    ) -> str:
        body: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": DEEPSEEK_TEMPERATURE if temperature is None else temperature,
            "max_tokens": max_tokens or DEEPSEEK_MAX_TOKENS,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        data = await self._post("/chat/completions", body)
        choices = data.get("choices") or []
        if not choices:
            raise DeepSeekError(f"DeepSeek 未返回 choices: {str(data)[:200]}")
        content = (choices[0].get("message") or {}).get("content") or ""
        logger.info(
            "deepseek: model=%s finish=%s tokens=%s",
            body["model"], choices[0].get("finish_reason"),
            (data.get("usage") or {}).get("total_tokens"),
        )
        return content

    async def chat_json(self, messages: List[Dict[str, str]], **kw: Any) -> Any:
        return parse_json_content(await self.chat(messages, json_mode=True, **kw))

    # ------------------------------------------------------------------ 自检
    async def verify(self) -> Dict[str, Any]:
        """极小请求验证 key. 返回 {ok, model, latency_ms, reply} 或抛异常."""
        t0 = time.time()
        reply = await self.chat(
            [{"role": "user", "content": "回复两个字: 正常"}],
            temperature=0.0,
            max_tokens=DEEPSEEK_VERIFY_MAX_TOKENS,
        )
        return {
            "ok": True,
            "model": self.model,
            "base_url": self.base_url,
            "latency_ms": int((time.time() - t0) * 1000),
            "reply": (reply or "").strip()[:40],
            "key_masked": mask_key(self.api_key),
        }
