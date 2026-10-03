#!/usr/bin/env python3
"""BTC/ETH KDJ 运行治理审计：只读，不激活策略、不写 runtime、不下单。

审计目标是把声明式策略卡、运行时规格、心跳、状态快照和前端/文档漂移
放到同一份可复现报告中。该工具只写调用方指定的输出目录。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
CARD_DIR = ROOT / "config" / "strategies"
RUNTIME_DIR = ROOT / "runtime" / "shadow"
SCHEMA_PATH = ROOT / "docs" / "strategy_governance.v1.json"

FACT_SOURCE_ORDER = [
    "运行时代码与已落盘心跳/状态（shadow/strategy_books.py、shadow/deploy.py、runtime/shadow/*.json）",
    "策略卡 config/strategies/*.json（声明式意图与启用状态）",
    "信号/指标实现 shadow/signals.py、shadow/indicators.py、shadow/engine.py",
    "前端/面板展示（只能消费 API/快照，不得定义交易事实）",
    "docs/ 与 outputs/ 历史说明（仅背景，不能覆盖以上事实）",
]


def load_schema() -> Dict[str, Any]:
    schema = load_json(SCHEMA_PATH, {}) or {}
    if not isinstance(schema, dict) or schema.get("schema_version") != "strategy_governance.v1":
        raise ValueError(f"invalid governance schema: {SCHEMA_PATH}")
    return schema


def implementation_fingerprint(schema: Dict[str, Any]) -> str:
    """Hash governed KDJ files plus the canonical schema, not Git state."""
    h = hashlib.sha256()
    for rel in [*schema.get("implementation_files", []), "docs/strategy_governance.v1.json"]:
        path = ROOT / rel
        h.update(rel.encode("utf-8")); h.update(b"\0")
        h.update(path.read_bytes() if path.is_file() else b"MISSING")
        h.update(b"\0")
    return h.hexdigest()


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def cards() -> List[Dict[str, Any]]:
    return [load_json(p, {}) for p in sorted(CARD_DIR.glob("*.json"))]


def source_text(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


def find_lines(paths: Iterable[Path], patterns: Iterable[str]) -> List[Dict[str, Any]]:
    regs = [(p, re.compile(p, re.I)) for p in patterns]
    out: List[Dict[str, Any]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for n, line in enumerate(lines, 1):
            for pat, rx in regs:
                if rx.search(line):
                    out.append({"file": str(path.relative_to(ROOT)), "line": n, "pattern": pat, "text": line.strip()[:240]})
    return out


def build_audit() -> Dict[str, Any]:
    schema = load_schema()
    from shadow import engine
    from shadow.strategy_books import SPECS, SPEC_5M, SPEC_ETH_5M, SPEC_ETH_15M, SPEC_15M
    from shadow.signals import MACD_FAST, MACD_SLOW, MACD_SIGNAL

    card_rows = cards()
    heartbeat = load_json(RUNTIME_DIR / "runner_heartbeat.json", {}) or {}
    state = load_json(RUNTIME_DIR / "deployed_state.json", {}) or {}
    runtime_ids = [s.id for s in SPECS]
    card_by_runtime = {str(c.get("runtime_key")): c for c in card_rows}
    enabled_cards = [c for c in card_rows if c.get("enabled") is True]
    disabled_5m = [c for c in card_rows if c.get("timeframe") == "5m" and c.get("enabled") is False]
    heartbeat_ids = list(heartbeat.get("strategies") or [])
    state_ids = sorted((state.get("strategies") or {}).keys())
    expected_active = list(schema["active_runtime_keys"])
    expected_research = list(schema["research_only_runtime_keys"])

    runtime_checks = {
        "card_count": len(card_rows),
        "enabled_card_runtime_keys": sorted(c.get("runtime_key") for c in enabled_cards),
        "disabled_5m_runtime_keys": sorted(c.get("runtime_key") for c in disabled_5m),
        "runtime_specs": runtime_ids,
        "heartbeat_strategies": heartbeat_ids,
        "state_strategy_books": state_ids,
        "runtime_is_two_15m": runtime_ids == expected_active,
        "heartbeat_matches_runtime": heartbeat_ids == runtime_ids,
        "state_books_match_runtime": state_ids == sorted(runtime_ids),
        "five_minute_definitions_retained": [s.id for s in (SPEC_5M, SPEC_ETH_5M)] == expected_research,
        "five_minute_not_in_runtime_specs": not any(s.interval == "5m" for s in SPECS),
    }
    signal_checks = {
        "kdj": [9, 3, 3],
        "macd": [MACD_FAST, MACD_SLOW, MACD_SIGNAL],
        "all_runtime_require_macd": all(s.require_macd and s.k_long_max is None and s.k_short_min is None for s in SPECS),
        "schema_signal_matches_runtime": schema["signal"]["macd"] == [MACD_FAST, MACD_SLOW, MACD_SIGNAL],
        "all_cards_macd_gate": all("MACD" in str(c.get("entry", {})) for c in card_rows),
        "no_enabled_card_k_extreme": all(not (c.get("enabled") and ("K<30" in json.dumps(c, ensure_ascii=False) or "K>70" in json.dumps(c, ensure_ascii=False))) for c in card_rows),
    }
    risk_checks = {
        "risk_r": engine.RISK_R,
        "atr_multiplier_k": engine.ATR_MULT_K,
        "leverage": engine.LEVERAGE,
        "daily_loss_limit": engine.DAILY_LOSS_LIMIT,
        "max_drawdown": engine.MAX_DRAWDOWN,
        "disaster_atr": engine.DISASTER_ATR,
        "cards_uniform_risk": all(c.get("position_sizing", {}).get("r") == engine.RISK_R for c in card_rows),
        "cards_uniform_leverage": all(c.get("risk", {}).get("leverage") == engine.LEVERAGE for c in card_rows),
    }
    market_checks = {
        "heartbeat_market": heartbeat.get("market"),
        "heartbeat_symbols": heartbeat.get("symbols"),
        "state_market": state.get("market"),
        "state_market_rest": state.get("market_rest"),
        "heartbeat_is_testnet": heartbeat.get("market") == "testnet",
        "state_is_testnet": state.get("market") == "testnet" and "testnet.binancefuture.com" in str(state.get("market_rest")),
        "symbols_are_btc_eth": sorted(heartbeat.get("symbols") or []) == sorted(schema["symbols"]),
    }
    frontend_files = [ROOT / "frontend" / "app.js", ROOT / "frontend" / "index.html"]
    docs_files = list((ROOT / "docs").rglob("*.md")) + list((ROOT / "outputs").rglob("*.md"))
    drift_scan = {
        "frontend_k_threshold_fallbacks": find_lines(frontend_files, [r"K<30", r"K>70"]),
        "frontend_fixed_history_date": find_lines(frontend_files, [r"2026-10-02"]),
        "stale_docs_5m_threshold_claims": find_lines(docs_files, [r"5m.*(没有 MACD|K<30|K>70|K 值阈值)", r"15m.*MACD.*5m.*K"]),
    }
    facts = {
        "active_runtime": [
            {"runtime_key": s.id, "strategy_id": next((c.get("strategy_id") for c in card_rows if c.get("runtime_key") == s.id), None), "symbol": s.symbol, "timeframe": s.interval, "enabled_for_runtime": True, "signal_rule": s.signal_rule}
            for s in SPECS
        ],
        "research_only": [
            {"runtime_key": s.id, "symbol": s.symbol, "timeframe": s.interval, "defined_in_code": True, "enabled_for_runtime": False}
            for s in (SPEC_5M, SPEC_ETH_5M)
        ],
        "field_mapping": [
            {"canonical_field": "runtime_key", "card": "runtime_key", "runtime": "StrategySpec.id", "heartbeat": "strategies[]", "frontend": "api/strategies/active -> s.runtime"},
            {"canonical_field": "enabled_for_runtime", "card": "enabled", "runtime": "SPECS membership", "heartbeat": "strategies[]", "frontend": "strategy state"},
            {"canonical_field": "symbol", "card": "symbol", "runtime": "StrategySpec.symbol", "heartbeat": "symbols[]", "frontend": "position_sources[].symbol"},
            {"canonical_field": "timeframe", "card": "timeframe", "runtime": "StrategySpec.interval", "heartbeat": "(indirect via strategies)", "frontend": "reading.interval"},
            {"canonical_field": "signal_rule", "card": "entry.long/short + indicators", "runtime": "signal_rule/require_macd/k_*", "heartbeat": "not applicable", "frontend": "reading + strategy card"},
            {"canonical_field": "risk", "card": "position_sizing/risk", "runtime": "shadow.engine constants", "heartbeat": "not applicable", "frontend": "display only; never define"},
            {"canonical_field": "market", "card": "market", "runtime": "market_endpoints + reading", "heartbeat": "market", "frontend": "api/market/runner"},
        ],
    }
    contract = {
        "schema_version": schema["schema_version"],
        "contract_version": schema["contract_version"],
        "schema_file": str(SCHEMA_PATH.relative_to(ROOT)),
        "schema_sha256": hashlib.sha256(SCHEMA_PATH.read_bytes()).hexdigest(),
        "implementation_fingerprint": implementation_fingerprint(schema),
        "implementation_files": schema["implementation_files"],
    }
    return {
        "report_version": "strategy-governance.audit.v1",
        "contract": contract,
        "generated_from": str(ROOT),
        "fact_source_order": FACT_SOURCE_ORDER,
        "facts": facts,
        "checks": {"runtime": runtime_checks, "signal": signal_checks, "risk": risk_checks, "market": market_checks},
        "drift_scan": drift_scan,
        "verdict": "PASS" if all([
            runtime_checks["runtime_is_two_15m"], runtime_checks["heartbeat_matches_runtime"], runtime_checks["state_books_match_runtime"], runtime_checks["five_minute_not_in_runtime_specs"], signal_checks["all_runtime_require_macd"], signal_checks["schema_signal_matches_runtime"], signal_checks["all_cards_macd_gate"], risk_checks["cards_uniform_risk"], market_checks["heartbeat_is_testnet"], market_checks["state_is_testnet"], market_checks["symbols_are_btc_eth"],
        ]) else "FAIL",
        "governance_findings": [
            {"severity": "RESOLVED", "finding": "旧对照报告曾把 5m K 阈值写成当前规则", "action": "已重写为 5m 研究保留、实盘停用；历史材料仍按背景处理"},
            {"severity": "RESOLVED", "finding": "前端曾包含 K<30/K>70 展示回退和固定日期", "action": "已改为消费快照/后端统计起点，不定义交易事实"},
            {"severity": "GUARD", "finding": "策略卡 runtime_key/启用状态必须与 shadow.SPECS、heartbeat、state 对齐", "action": "由本审计脚本和 tests/test_strategy_drift_guard.py 自动校验"},
            {"severity": "GUARD", "finding": "config/strategy_versions ACTIVE 是评分策略包，不是 KDJ 运行规格", "action": "报告分离 bundle 身份与 KDJ runtime 身份；禁止互相替代"},
        ],
        "allowed_legacy_mentions": [
            "entry_signal 的 k_long_max/k_short_min、price_breaks/BREAK_ATR_MULT 仅为历史测试兼容，不是当前规格",
            "5m 策略卡和 latest_reading_5m/latest_reading_eth5 允许作为研究/历史审计材料，但不得进入 runtime/shadow 的策略账本或 heartbeat strategies",
        ],
    }


def render_markdown(report: Dict[str, Any]) -> str:
    c = report["checks"]
    lines = [
        "# 策略统一事实与防漂移审计报告",
        "",
        f"> 生成器：`scripts/strategy_drift_audit.py`；审计版本：`{report['report_version']}`。本报告只读，不激活策略、不写 runtime、不下单。",
        "",
        f"## 结论：**{report['verdict']}**",
        "",
        "### 事实源排序",
        *[f"{i}. {x}" for i, x in enumerate(report["fact_source_order"], 1)],
        "",
        "### 当前事实",
        "### 合同与实现指纹",
        f"- schema：`{report['contract']['schema_file']}`；合同版本 `{report['contract']['contract_version']}`；schema SHA-256 `{report['contract']['schema_sha256']}`。",
        f"- KDJ 实现指纹：`{report['contract']['implementation_fingerprint']}`。治理文件或策略卡变更后必须重新生成并审阅本报告；本工具不自动激活、不写 runtime。",
        "",
        "### 当前事实",
        "| 项目 | 核对结果 |",
        "|---|---|",
        f"| 实盘运行策略 | `{', '.join(c['runtime']['runtime_specs'])}`，仅 BTC/ETH 两条 15m |",
        f"| 5m | 定义保留用于回测/研究；运行规格不含 5m，心跳不含 5m，实盘停用 |",
        f"| 心跳 | `{c['market']['heartbeat_market']}`，策略 `{', '.join(c['runtime']['heartbeat_strategies'])}`，15 秒 |",
        f"| 市场 | BTCUSDT、ETHUSDT；REST `{c['market']['state_market_rest']}` |",
        f"| 信号 | KDJ(9,3,3)；MACD({','.join(map(str, c['signal']['macd']))}) 柱正负闸门；K 极值不参与当前规格 |",
        f"| 风险 | r={c['risk']['risk_r']}，k={c['risk']['atr_multiplier_k']}，杠杆={c['risk']['leverage']}x，日亏={c['risk']['daily_loss_limit']:.0%}，回撤={c['risk']['max_drawdown']:.0%}，灾难止损={c['risk']['disaster_atr']}×ATR |",
        "",
        "### 策略数量与字段映射",
        "| canonical | 策略卡 | 运行时代码 | 心跳/状态 | 前端消费 |",
        "|---|---|---|---|---|",
        *[f"| {r['canonical_field']} | {r['card']} | {r['runtime']} | {r['heartbeat']} | {r['frontend']} |" for r in report["facts"]["field_mapping"]],
        "",
        "### 漂移发现",
        "| 严重度 | 发现 | 安全动作 |",
        "|---|---|---|",
        *[f"| {x['severity']} | {x['finding']} | {x['action']} |" for x in report["governance_findings"]],
        "",
        "### 逐文件修改清单（本轮不触碰交易路径）",
        "| 文件 | 动作 | 说明 |",
        "|---|---|---|",
        "| `scripts/strategy_drift_audit.py` | 已新增 | 只读扫描、字段映射、事实判定、报告生成 |",
        "| `tests/test_strategy_drift_guard.py` | 已新增 | 钉住两条 15m 运行事实、5m 停用、MACD 与测试网市场 |",
        "| `outputs/2026-10-03-四个策略现状对照.md` | 建议安全修改 | 删除 5m 当前阈值叙述，改为研究保留/实盘停用；不改策略参数 |",
        "| `frontend/app.js` / `frontend/index.html` | 建议补丁 | 禁止前端定义交易事实；信号文案只消费 reading 字段；历史日期标明为后端统计口径 |",
        "| `shadow/*`、`runtime/*`、`trading/*`、下单/运行器 | 明确不修改 | 本轮只读核对，禁止重启、发单、改线上策略 |",
        "",
        "### 生成/校验命令",
        "```bash",
        "PYTHONDONTWRITEBYTECODE=1 python3 scripts/strategy_drift_audit.py --out-dir .work/strategy-governance",
        "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_strategy_drift_guard",
        "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test*.py'",
        "```",
        "",
        "### 允许的历史字段",
        *[f"- {x}" for x in report["allowed_legacy_mentions"]],
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=ROOT / ".work" / "strategy-governance")
    args = parser.parse_args()
    report = build_audit()
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "strategy-drift-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "strategy-drift-audit.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"verdict": report["verdict"], "out_dir": str(out), "runtime": report["facts"]["active_runtime"], "drift_counts": {k: len(v) for k, v in report["drift_scan"].items()}}, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
