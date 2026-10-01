"""档案存储与统计单测 — 重点钉死「分母是有效样本」这条口径."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from models.review import SettleStatus, TradeRecord
from review.journal import TradeJournal, new_trade_id
from review.stats import compute_stats


def rec(trade_id: str, *, status: str, cs: float = 50.0, horizon: str = "short_term",
        decision: str = "STANDARD_LONG", opened_at_ms: int = 0,
        news: float = 0.0, data: float = 0.0, tech: float = 0.0,
        prediction: float = 0.0, mfe=None, mae=None, settled_at_ms=None) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        opened_at_ms=opened_at_ms,
        horizon=horizon,
        entry_price=100.0,
        scores={"news": news, "data": data, "tech": tech, "prediction": prediction},
        composite_score=cs,
        decision=decision,
        status=status,
        max_favorable_atr=mfe,
        max_adverse_atr=mae,
        settled_at_ms=settled_at_ms,
    )


class TestJournalStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "journal.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_then_reload_round_trip(self):
        j = TradeJournal(self.path)
        j.append(rec("T1", status=SettleStatus.CORRECT.value, opened_at_ms=1000))
        j.append(rec("T2", status=SettleStatus.WRONG.value, opened_at_ms=2000))
        self.assertEqual(len(j), 2)

        fresh = TradeJournal(self.path).load()
        got = fresh.get("T2")
        self.assertIsNotNone(got)
        self.assertEqual(got.status, SettleStatus.WRONG.value)
        self.assertEqual([r.trade_id for r in fresh.all()], ["T1", "T2"])

    def test_update_persists_to_disk(self):
        j = TradeJournal(self.path)
        r = j.append(rec("T1", status=SettleStatus.PENDING.value))
        r.status = SettleStatus.CORRECT.value
        r.exit_price = 110.0
        r.note = "手补"
        j.update(r)

        fresh = TradeJournal(self.path).load()
        self.assertEqual(fresh.get("T1").exit_price, 110.0)
        self.assertEqual(fresh.get("T1").note, "手补")

    def test_records_sorted_by_open_time_not_insert_order(self):
        j = TradeJournal(self.path)
        j.append(rec("T-late", status=SettleStatus.PENDING.value, opened_at_ms=9000))
        j.append(rec("T-early", status=SettleStatus.PENDING.value, opened_at_ms=1000))
        self.assertEqual([r.trade_id for r in j.all()], ["T-early", "T-late"])

    def test_corrupt_line_skipped_not_fatal(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        good = rec("T1", status=SettleStatus.CORRECT.value).to_dict()
        with self.path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(good, ensure_ascii=False) + "\n")
            fh.write("{ 这不是 json\n")
            fh.write("\n")
            fh.write(json.dumps(rec("T2", status=SettleStatus.WRONG.value).to_dict(),
                                ensure_ascii=False) + "\n")

        j = TradeJournal(self.path).load()
        self.assertEqual(sorted(r.trade_id for r in j.all()), ["T1", "T2"])

    def test_record_without_id_gets_one_on_load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = rec("", status=SettleStatus.PENDING.value, opened_at_ms=555).to_dict()
        with self.path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

        j = TradeJournal(self.path).load()
        got = j.all()[0]
        self.assertTrue(got.trade_id)
        self.assertTrue(got.trade_id.startswith("T555-"))

    def test_unknown_fields_tolerated(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = rec("T1", status=SettleStatus.PENDING.value).to_dict()
        payload["some_future_field"] = {"a": 1}
        with self.path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

        self.assertEqual(len(TradeJournal(self.path).load()), 1)

    def test_filters_and_pending_and_errors(self):
        j = TradeJournal(self.path)
        j.append(rec("C1", status=SettleStatus.CORRECT.value, opened_at_ms=1))
        j.append(rec("W1", status=SettleStatus.WRONG.value, opened_at_ms=2))
        j.append(rec("W2", status=SettleStatus.WRONG.value, horizon="long_term", opened_at_ms=3))
        j.append(rec("P1", status=SettleStatus.PENDING.value, opened_at_ms=4))

        self.assertEqual([r.trade_id for r in j.filter(errors_only=True)], ["W2", "W1"])
        self.assertEqual([r.trade_id for r in j.filter(horizon="long_term")], ["W2"])
        self.assertEqual([r.trade_id for r in j.pending()], ["P1"])
        self.assertEqual([r.trade_id for r in j.errors()], ["W1", "W2"])

    def test_filter_limit_returns_newest_first(self):
        j = TradeJournal(self.path)
        for i in range(5):
            j.append(rec(f"T{i}", status=SettleStatus.CORRECT.value, opened_at_ms=i))
        self.assertEqual([r.trade_id for r in j.filter(limit=2)], ["T4", "T3"])

    def test_new_trade_id_shape(self):
        tid = new_trade_id(1_700_000_000_000)
        self.assertTrue(tid.startswith("T1700000000000-"))
        self.assertEqual(len(tid.split("-")[1]), 6)


class TestComputeStats(unittest.TestCase):
    def test_denominator_is_valid_samples_not_trade_count(self):
        records = [
            rec("C1", status=SettleStatus.CORRECT.value),
            rec("C2", status=SettleStatus.CORRECT.value),
            rec("W1", status=SettleStatus.WRONG.value),
            rec("I1", status=SettleStatus.INVALID.value),      # 横盘, 不进分母
            rec("I2", status=SettleStatus.INVALID.value),
            rec("E1", status=SettleStatus.EXCLUDED.value),      # 被覆盖, 不进分母
            rec("P1", status=SettleStatus.PENDING.value),       # 未走完窗口
        ]
        st = compute_stats(records, target=100)
        self.assertEqual(st.total, 7)
        self.assertEqual(st.valid, 3)                 # 2 对 + 1 错
        self.assertEqual(st.correct, 2)
        self.assertEqual(st.wrong, 1)
        self.assertEqual(st.invalid, 2)
        self.assertEqual(st.excluded, 1)
        self.assertEqual(st.pending, 1)
        self.assertAlmostEqual(st.win_rate, 2 / 3, places=4)
        self.assertEqual(st.remaining, 97)
        self.assertFalse(st.ready)

    def test_ready_only_when_valid_reaches_target(self):
        small = [rec(f"C{i}", status=SettleStatus.CORRECT.value) for i in range(4)]
        st = compute_stats(small, target=5)
        self.assertEqual(st.remaining, 1)
        self.assertFalse(st.ready)

        small.append(rec("C4", status=SettleStatus.CORRECT.value))
        st = compute_stats(small, target=5)
        self.assertEqual(st.remaining, 0)
        self.assertTrue(st.ready)

    def test_invalid_chop_cannot_pad_the_denominator(self):
        # 100 笔全横盘 → 有效样本仍是 0, 不该触发复盘
        records = [rec(f"I{i}", status=SettleStatus.INVALID.value) for i in range(100)]
        st = compute_stats(records, target=100)
        self.assertEqual(st.valid, 0)
        self.assertFalse(st.ready)
        self.assertIsNone(st.win_rate)

    def test_win_rate_none_when_no_valid_samples(self):
        st = compute_stats([rec("P1", status=SettleStatus.PENDING.value)], target=100)
        self.assertIsNone(st.win_rate)

    def test_by_horizon_and_by_tier_split(self):
        records = [
            rec("A", status=SettleStatus.CORRECT.value, horizon="short_term", decision="STANDARD_LONG"),
            rec("B", status=SettleStatus.WRONG.value, horizon="short_term", decision="STANDARD_LONG"),
            rec("C", status=SettleStatus.CORRECT.value, horizon="long_term", decision="WATCH_LONG"),
        ]
        st = compute_stats(records)
        self.assertEqual(st.by_horizon["short_term"]["valid"], 2)
        self.assertEqual(st.by_horizon["short_term"]["correct"], 1)
        self.assertAlmostEqual(st.by_horizon["short_term"]["win_rate"], 0.5)
        self.assertEqual(st.by_tier["WATCH_LONG"]["win_rate"], 1.0)

    def test_by_direction_uses_session_direction_and_pnl(self):
        records = [
            rec("L", status=SettleStatus.CORRECT.value, cs=60, mfe=2.0, mae=0.2),
            rec("S", status=SettleStatus.WRONG.value, cs=-60, mfe=0.1, mae=2.0),
        ]
        st = compute_stats(records)
        self.assertEqual(st.by_direction["LONG"]["valid"], 1)
        self.assertEqual(st.by_direction["SHORT"]["valid"], 1)
        self.assertAlmostEqual(st.by_direction["LONG"]["pnl_atr_avg"], 1.8, places=3)
        self.assertAlmostEqual(st.by_direction["SHORT"]["pnl_atr_avg"], -1.9, places=3)

    def test_face_means_reveal_which_face_lies(self):
        # 消息面在「错」的一组里读数明显更极端 → delta 应为负且明显
        records = [
            rec("C1", status=SettleStatus.CORRECT.value, news=5, data=40, tech=30, prediction=10),
            rec("C2", status=SettleStatus.CORRECT.value, news=15, data=40, tech=30, prediction=10),
            rec("W1", status=SettleStatus.WRONG.value, news=-70, data=40, tech=30, prediction=10),
            rec("W2", status=SettleStatus.WRONG.value, news=-50, data=40, tech=30, prediction=10),
        ]
        st = compute_stats(records)
        news = st.face_means["news"]
        self.assertEqual(news["label"], "消息面")
        self.assertAlmostEqual(news["correct"], 10.0, places=2)
        self.assertAlmostEqual(news["wrong"], -60.0, places=2)
        self.assertAlmostEqual(news["delta"], 70.0, places=2)
        # 数据面两组一样 → 差为 0, 说明它没在骗人
        self.assertAlmostEqual(st.face_means["data"]["delta"], 0.0, places=2)

    def test_face_means_none_when_group_empty(self):
        st = compute_stats([rec("C1", status=SettleStatus.CORRECT.value, news=10)])
        self.assertIsNone(st.face_means["news"]["wrong"])
        self.assertIsNone(st.face_means["news"]["delta"])

    def test_errors_since_last_review_counts_all_when_no_since(self):
        records = [
            rec("W1", status=SettleStatus.WRONG.value),
            rec("W2", status=SettleStatus.WRONG.value),
            rec("C1", status=SettleStatus.CORRECT.value),
        ]
        st = compute_stats(records)
        self.assertEqual(st.errors_since_last_review, 2)

    def test_errors_since_last_review_respects_timestamp(self):
        records = [
            rec("W-old", status=SettleStatus.WRONG.value, settled_at_ms=1000),
            rec("W-new", status=SettleStatus.WRONG.value, settled_at_ms=5000),
        ]
        st = compute_stats(records, since_ms=3000)
        self.assertEqual(st.errors_since_last_review, 1)

    def test_to_dict_is_serializable(self):
        st = compute_stats([rec("C1", status=SettleStatus.CORRECT.value, news=10)])
        json.dumps(st.to_dict(), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
