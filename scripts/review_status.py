"""复盘回路 CLI — 查状态、补档案、结算、跑模型、采纳/回滚.

用法示例:
    python3 scripts/review_status.py status
    python3 scripts/review_status.py add --price 63000 --atr 400 --data 54 --tech 62
    python3 scripts/review_status.py settle
    python3 scripts/review_status.py simulate --n 110 --journal runtime/review/sim_journal.jsonl
    python3 scripts/review_status.py run --force            # 统计引擎, 无需任何 Key
    python3 scripts/review_status.py accept P20260913-abc123
    python3 scripts/review_status.py versions
    python3 scripts/review_status.py rollback v1

演练建议用 --journal 指向独立档案, 别把合成样本混进真实档案。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.review import ACTIONABLE_DECISIONS, SETTLE_CONFIG, VALID_SAMPLE_TARGET  # noqa: E402
from engine.scorer import FactorScoringEngine                        # noqa: E402
from models.review import SettleStatus, TradeRecord, now_ms          # noqa: E402
from models.signals import StrategyHorizon, DimensionScores          # noqa: E402
from review import overrides                                         # noqa: E402
from review import review_loop as rl                                 # noqa: E402
from review import statistical_review as sr                          # noqa: E402
from review.journal import TradeJournal, new_trade_id                # noqa: E402
from review.settle import settle_pending                             # noqa: E402
from review.stats import compute_stats                               # noqa: E402


def _journal(args) -> TradeJournal:
    return TradeJournal(Path(args.journal)) if args.journal else TradeJournal()


# --------------------------------------------------------------------------- status
def cmd_status(args) -> int:
    journal = _journal(args)
    records = journal.all()
    stats = compute_stats(records)
    last = rl.latest_decided_proposal()

    print(f"档案:      {journal.path}")
    print(f"配置版本:  {overrides.current_version_label()}")
    print(f"总数:      {stats.total}")
    print(f"  有效 (对+错): {stats.valid}   距达标还差 {stats.remaining} / {stats.target}")
    print(f"  对 {stats.correct}   错 {stats.wrong}   无效(横盘) {stats.invalid}   "
          f"待结算 {stats.pending}   排除 {stats.excluded}")
    if stats.win_rate is not None:
        print(f"  胜率: {stats.win_rate:.1%}")

    bar_len = 50
    filled = min(bar_len, int(bar_len * stats.valid / max(stats.target, 1)))
    print(f"  [{'█' * filled}{'·' * (bar_len - filled)}] {stats.valid}/{stats.target}"
          f"{'  ✅ 可以复盘' if stats.ready else ''}")

    if stats.by_direction:
        print("分方向:")
        for d, b in stats.by_direction.items():
            wr = b.get("win_rate")
            print(f"  {d:<7} n={b['valid']:<4} 胜率 "
                  f"{'—' if wr is None else f'{wr:.1%}'}  均 MFE-MAE {b.get('pnl_atr_avg')} ATR")
    if stats.by_tier:
        print("分档位:")
        for t, b in sorted(stats.by_tier.items()):
            wr = b.get("win_rate")
            print(f"  {t:<15} n={b['valid']:<4} 胜率 {'—' if wr is None else f'{wr:.1%}'}")
    if stats.total:
        rate = stats.valid / stats.total
        print(f"上下文: {stats.total} 笔记录里只有 {stats.valid} 笔真开仓 "
              f"({rate:.0%}) — 照这个比例, 攒够 {stats.target} 笔有效样本"
              f"约需 {int(stats.target / max(rate, 1e-9))} 笔成交")
    if stats.face_means:
        print("分面读数 (对组 vs 错组):")
        print(f"  {'':<5} {'带符号均值':<20} {'幅度均值':<20}")
        for k, m in stats.face_means.items():
            print(f"  {m['label']:<5} "
                  f"对 {m['correct']} / 错 {m['wrong']} (差 {m['delta']})".ljust(30)
                  + f"对 {m['abs_correct']} / 错 {m['abs_wrong']} (差 {m['abs_delta']})")
    if last:
        print(f"上版建议: {last.proposal_id} ({last.status}) 之后新增错单 {stats.errors_since_last_review}")
    return 0


# --------------------------------------------------------------------------- add
def _build_record(args) -> TradeRecord:
    horizon = args.horizon
    engine = FactorScoringEngine()
    overrides.apply_to_engine(engine)
    dim = DimensionScores(news=args.news, data=args.data, tech=args.tech, prediction=args.pred)
    result = engine.evaluate(StrategyHorizon(horizon), dim)
    opened = args.opened_at_ms or now_ms()
    return TradeRecord(
        trade_id=new_trade_id(opened),
        opened_at_ms=opened,
        horizon=horizon,
        entry_price=args.price,
        scores={"news": dim.news, "data": dim.data, "tech": dim.tech, "prediction": dim.prediction},
        composite_score=result.composite_score,
        decision=result.decision.value,
        atr=args.atr,
        safety_valve=result.safety_valve_triggered,
        overridden=args.overridden,
        source=args.source,
        note=args.note,
        config_version=overrides.current_version_label(),
    )


def cmd_add(args) -> int:
    journal = _journal(args)
    rec = journal.append(_build_record(args))
    print(f"已记录 {rec.trade_id}")
    print(f"  四面 {rec.scores}")
    print(f"  CS {rec.composite_score} → {rec.decision}  方向 {rec.direction}")
    return 0


# --------------------------------------------------------------------------- settle
def cmd_settle(args) -> int:
    journal = _journal(args)
    summary = asyncio.run(settle_pending(journal, horizon=args.horizon))
    print(f"待结算 {summary['considered']} 笔")
    print(f"  取到 K 线: {summary['fetched']}")
    print(f"  结果: {summary['settled']}")
    return 0


# --------------------------------------------------------------------------- simulate
def cmd_simulate(args) -> int:
    """生成合成样本, 用来演练「攒够 N 笔有效样本 → 复盘」的整条链路 (不碰真实档案).

    --n 指的是**有效样本**数 (对+错), 不是写入笔数: 四面模型会产出大量「观望」
    读数, 那些没开仓、不进分母, 所以真实写入笔数会明显多于 n。这个差值本身就是
    有用的信息, 会一并打印出来。
    """
    journal = _journal(args)
    rng = random.Random(args.seed)
    engine = FactorScoringEngine()
    overrides.apply_to_engine(engine)
    # 故意埋一个真问题: 消息面读数越极端, 越容易判错 → 让模型有事可做
    now = now_ms()
    hour = 3_600_000
    max_attempts = max(args.n * 60, 800)
    valid = 0
    created = 0
    batch: List[TradeRecord] = []

    while valid < args.n and created < max_attempts:
        horizon = "short_term" if rng.random() < 0.8 else "long_term"
        # 四面读数都围绕 0 铺开, 让 CS 同时落在多空两侧 (否则只会出现多单, 空单那半边链路练不到)
        news = rng.choice([-70, -40, -15, 0, 15, 40, 70]) + rng.gauss(0, 8)
        data = rng.gauss(6, 50)
        tech = rng.gauss(4, 46)
        pred = rng.gauss(4, 42)
        dim = DimensionScores(news=_b(news), data=_b(data), tech=_b(tech), prediction=_b(pred))
        result = engine.evaluate(StrategyHorizon(horizon), dim)

        opened = now - int((max_attempts - created) * 6 * hour)
        rec = TradeRecord(
            trade_id=new_trade_id(opened),
            opened_at_ms=opened,
            horizon=horizon,
            entry_price=round(60000 + rng.gauss(0, 4000), 2),
            scores={"news": dim.news, "data": dim.data, "tech": dim.tech, "prediction": dim.prediction},
            composite_score=result.composite_score,
            decision=result.decision.value,
            atr=round(300 + rng.random() * 250, 2),
            safety_valve=result.safety_valve_triggered,
            overridden=rng.random() < 0.02,
            source="simulated",
            note="",
            config_version=overrides.current_version_label(),
        )
        _assign_synthetic_outcome(rec, rng)
        batch.append(rec)
        created += 1
        if rec.is_valid_sample:
            valid += 1

    journal.append_many(batch)
    stats = compute_stats(journal.all())
    print(f"已写入 {created} 笔合成样本 → {journal.path}")
    print(f"  有效 {stats.valid} (对 {stats.correct} / 错 {stats.wrong})  "
          f"无效 {stats.invalid}  排除 {stats.excluded}")
    if stats.win_rate is not None:
        print(f"  胜率 {stats.win_rate:.1%}")
    if not stats.ready:
        print(f"  ⚠ 未达 {stats.target} 笔有效样本 (还差 {stats.remaining}), "
              f"采样上限 {max_attempts} 笔已用尽")
    else:
        print(f"  (演练说明: 这里是随机撒四面读数, 所以绝大多数落在观望区间; "
              f"真实档案是「每成交一笔才记一条」, 有效样本占比会高得多)")
    return 0


def _b(v: float) -> float:
    return round(max(-100.0, min(100.0, v)), 1)


def _assign_synthetic_outcome(rec: TradeRecord, rng: random.Random) -> None:
    """按「消息面越极端越容易错」的隐含规律合成结算结果.

    排除条件与 settle.settle_record 保持一致: 观望/中性没开仓, 不进分母。
    """
    if rec.overridden:
        rec.status = SettleStatus.EXCLUDED.value
        rec.settle_detail = {"reason": "synthetic_excluded"}
        return
    if rec.decision not in ACTIONABLE_DECISIONS:
        rec.status = SettleStatus.EXCLUDED.value
        rec.settle_detail = {"reason": "synthetic_no_position", "decision": rec.decision}
        return
    tilt = abs(rec.scores.get("news", 0)) / 100.0
    p_correct = 0.62 - 0.30 * tilt
    if rec.direction == "SHORT":
        p_correct -= 0.06                      # BTC 长期偏多, 做空天然吃亏
    roll = rng.random()
    if roll < 0.10:
        rec.status = SettleStatus.INVALID.value
        rec.settle_detail = {"reason": "synthetic_chop"}
        rec.max_favorable_atr = round(rng.uniform(0, 0.9), 2)
        rec.max_adverse_atr = round(rng.uniform(0, 0.9), 2)
        return
    correct = rng.random() < p_correct
    rec.status = (SettleStatus.CORRECT if correct else SettleStatus.WRONG).value
    rec.settled_at_ms = rec.opened_at_ms + int(2 * 3_600_000)
    rec.max_favorable_atr = round(rng.uniform(1.0, 3.2) if correct else rng.uniform(0, 0.8), 2)
    rec.max_adverse_atr = round(rng.uniform(0, 0.8) if correct else rng.uniform(1.0, 3.2), 2)
    rec.settle_detail = {"reason": "synthetic_target_first" if correct else "synthetic_stop_first"}


# --------------------------------------------------------------------------- 注解 / 复盘
def cmd_annotate(args) -> int:
    journal = _journal(args)
    targets = ([r for r in journal.errors() if not r.model_note]
               if args.all else [journal.get(args.trade_id)])
    targets = [t for t in targets if t]
    if not targets:
        print("没有需要分析的错单。")
        return 0

    stats = compute_stats(journal.all())
    for rec in targets:
        rec.model_note = sr.annotate_record(rec, {
            "valid": stats.valid, "win_rate": stats.win_rate,
            "face_means": stats.face_means,
        })
        journal.update(rec)
        print(f"{rec.trade_id}: {rec.model_note}")
    return 0


def cmd_run(args) -> int:
    journal = _journal(args)
    proposal = asyncio.run(rl.run_review(journal.all(), force=args.force))
    print(f"建议 {proposal.proposal_id}  [{proposal.status}]  引擎 {proposal.model}")
    print(f"有效样本 {proposal.valid_sample_count}")
    if proposal.blocked_reason:
        print(f"受阻: {proposal.blocked_reason}")
    if proposal.diagnosis:
        print(f"\n诊断:\n{proposal.diagnosis}")
    if proposal.changes:
        print("\n改动建议:")
        for c in proposal.changes:
            flag = "  [被护栏夹取]" if c.clamped else ""
            print(f"  {c.param}\n    {c.current} → {c.proposed}  ({c.delta_pct:+.1%}){flag}")
            if c.rationale:
                print(f"    理由: {c.rationale}")
            if c.expected_effect:
                print(f"    预期: {c.expected_effect}")
    if proposal.risks:
        print(f"\n风险:\n{proposal.risks}")
    if proposal.changes:
        print(f"\n确认请执行: python3 scripts/review_status.py accept {proposal.proposal_id}")
    return 0


def cmd_proposals(args) -> int:
    props = rl.load_proposals()
    if not props:
        print("还没有任何建议。")
        return 0
    for p in props:
        print(f"{p.proposal_id}  {p.status:<9} {p.model:<17} n={p.valid_sample_count:<4} "
              f"{len(p.changes)} 项  {p.applied_version or ''}")
    return 0


def cmd_accept(args) -> int:
    doc = rl.accept_proposal(args.proposal_id)
    print(f"已采纳 → 配置版本 {doc['version']} (父版本 {doc['parent']})")
    for c in doc["changes"]:
        print(f"  {c['param']}: {c['from']} → {c['to']}")
    print("旧版本已留档, 可随时 rollback。")
    return 0


def cmd_reject(args) -> int:
    rl.reject_proposal(args.proposal_id)
    print(f"已驳回 {args.proposal_id}")
    return 0


def cmd_versions(args) -> int:
    active = overrides.active_version_name()
    for v in overrides.list_versions():
        mark = "← 生效" if v["version"] == active else ""
        print(f"{v['version']:<5} {v.get('kind',''):<9} {len(v.get('changes', []))} 项改动  {mark}")
        for c in v.get("changes", []):
            print(f"      {c['param']}: {c['from']} → {c['to']}")
    return 0


def cmd_rollback(args) -> int:
    doc = overrides.rollback(args.version)
    print(f"已回滚到 {doc['version']}")
    return 0


# --------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="复盘回路 CLI")
    # 共用选项: 挂到每个子命令上, 这样 `status --journal X` 也能用
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--journal", help="档案路径 (默认 runtime/review/journal.jsonl)")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", parents=[common], help="查看复盘池状态").set_defaults(func=cmd_status)
    sub.add_parser("proposals", parents=[common], help="列出已产出的建议").set_defaults(func=cmd_proposals)
    sub.add_parser("versions", parents=[common], help="列出配置版本").set_defaults(func=cmd_versions)

    a = sub.add_parser("add", parents=[common], help="补录一笔成交")
    a.add_argument("--horizon", default="short_term", choices=list(SETTLE_CONFIG))
    a.add_argument("--price", type=float, required=True)
    a.add_argument("--atr", type=float)
    a.add_argument("--news", type=float, default=0.0)
    a.add_argument("--data", type=float, default=0.0)
    a.add_argument("--tech", type=float, default=0.0)
    a.add_argument("--pred", type=float, default=0.0)
    a.add_argument("--note", default="")
    a.add_argument("--source", default="manual")
    a.add_argument("--opened-at-ms", type=int, dest="opened_at_ms")
    a.add_argument("--overridden", action="store_true")
    a.set_defaults(func=cmd_add)

    s = sub.add_parser("settle", parents=[common], help="按事后价格结算所有待定档案")
    s.add_argument("--horizon", choices=list(SETTLE_CONFIG))
    s.set_defaults(func=cmd_settle)

    m = sub.add_parser("simulate", parents=[common], help="生成合成样本 (演练整条链路)")
    m.add_argument("--n", type=int, default=110)
    m.add_argument("--seed", type=int, default=7)
    m.set_defaults(func=cmd_simulate)

    n = sub.add_parser("annotate", parents=[common], help="让模型给错单写分析备注")
    n.add_argument("--all", action="store_true", help="批量补注所有未标注的错单")
    n.add_argument("--trade-id", dest="trade_id")
    n.set_defaults(func=cmd_annotate)

    r = sub.add_parser("run", parents=[common], help="用统计引擎产出参数微调建议 (无需 Key)")
    r.add_argument("--force", action="store_true", help="无视样本量门槛")
    r.set_defaults(func=cmd_run)

    ac = sub.add_parser("accept", parents=[common], help="采纳建议并写入新版本")
    ac.add_argument("proposal_id")
    ac.set_defaults(func=cmd_accept)

    rj = sub.add_parser("reject", parents=[common], help="驳回建议")
    rj.add_argument("proposal_id")
    rj.set_defaults(func=cmd_reject)

    rb = sub.add_parser("rollback", parents=[common], help="回滚到指定配置版本")
    rb.add_argument("version")
    rb.set_defaults(func=cmd_rollback)

    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "simulate" and not args.journal:
        # 演练默认走独立档案, 免得合成样本混进真实档案
        args.journal = "runtime/review/sim_journal.jsonl"
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
