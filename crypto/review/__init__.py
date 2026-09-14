"""复盘 / 自我改进回路.

模块分层:
    journal.py       — 交易档案存储 (JSONL, 追加为主)
    settle.py        — 按事后价格结算对错
    stats.py         — 复盘池统计
    prompt.py        — 构造送进模型的复盘 payload
    deepseek.py      — DeepSeek 客户端 (OpenAI 兼容)
    review_loop.py   — 触发条件 / 调用模型 / 护栏校验 / 采纳与版本留档
    overrides.py     — 生效配置的合并、留档与回滚
    panel_server.py  — 本机面板后端 (aiohttp)
"""
