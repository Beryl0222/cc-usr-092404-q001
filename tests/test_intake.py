import json
import shutil
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet.intake import (
    ACCEPTED,
    HELD_FUTURE,
    QUARANTINED,
    REVIEW,
    REPLAYED,
    IntakeService,
)


def packet(record_id="r1", revision=1, *, ver=1, domain="referral_packet",
           ts="2026-09-20T09:00:00+08:00", source="基层A", **extra):
    payload = {
        "schema_version": ver,
        "record_id": record_id,
        "domain": domain,
        "occurred_at": ts,
        "revision": revision,
        "source": source,
    }
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


class RecordingNotifier:
    def __init__(self, fail_times=0):
        self.delivered = []
        self.calls = 0
        self.fail_times = fail_times

    def deliver(self, accepted_event):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("通知通道不可用")
        receipt = f"ntf-{self.calls}"
        self.delivered.append((accepted_event["record_id"], accepted_event["revision"]))
        return receipt


class IntakeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.notifier = RecordingNotifier()
        self.svc = IntakeService(self.tmp, notifier=self.notifier)

    def tearDown(self):
        self.svc.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def reopen(self):
        self.svc.close()
        self.svc = IntakeService(self.tmp, notifier=self.notifier)
        return self.svc


class BatchOrderingTest(IntakeTestCase):
    def test_bad_record_only_quarantines_itself(self):
        items = [
            packet("a", 1),
            "{broken json",
            packet("b", revision=-1),
            packet("c", 1, ts="2026-09-20T09:00:00"),
            packet("d", 1, ver=2),
            packet("e", 1, ver=0, domain="referral",
                   ts="2026-09-20T09:00:00", timezone="+08:00"),
        ]
        result = self.svc.ingest_batch(items, batch_id="B1")
        outcomes = [o.outcome for o in result.outcomes]
        self.assertEqual(outcomes, [
            ACCEPTED, QUARANTINED, QUARANTINED, QUARANTINED, HELD_FUTURE, ACCEPTED,
        ])

    def test_legal_records_keep_input_order(self):
        items = [packet(f"r{i}", 1) for i in range(5)]
        result = self.svc.ingest_batch(items, batch_id="B2")
        self.assertEqual([o.record_id for o in result.accepted],
                         [f"r{i}" for i in range(5)])

    def test_quarantine_records_error_layer_and_location(self):
        result = self.svc.ingest_batch(["{x"], batch_id="B3")
        blocked = result.blocked[0]
        self.assertEqual(blocked.outcome, QUARANTINED)
        self.assertEqual(blocked.location, "B3 / #1")
        self.assertTrue(Path(blocked.raw_path).exists())

    def test_future_version_keeps_raw_and_blocks(self):
        result = self.svc.ingest_batch([packet("f", 1, ver=2)], batch_id="B4")
        outcome = result.outcomes[0]
        self.assertEqual(outcome.outcome, HELD_FUTURE)
        self.assertEqual(outcome.observed_version, 2)
        # 原文逐字留存。
        self.assertEqual(
            Path(outcome.raw_path).read_text("utf-8"), packet("f", 1, ver=2)
        )


class IdempotencyAndReviewTest(IntakeTestCase):
    def test_exact_retransmission_returns_prior_result(self):
        text = packet("a", 1)
        first = self.svc.ingest_batch([text], batch_id="I1")
        self.assertEqual(first.outcomes[0].outcome, ACCEPTED)
        second = self.svc.ingest_batch([text], batch_id="I2")
        self.assertEqual(second.outcomes[0].outcome, REPLAYED)
        self.assertEqual(second.outcomes[0].detail["prior_location"], "I1 / #1")
        # 不重复通知。
        self.assertEqual(self.notifier.delivered, [("a", 1)])

    def test_content_change_opens_review_and_does_not_overwrite(self):
        self.svc.ingest_batch([packet("a", 1, source="原版")], batch_id="R1")
        result = self.svc.ingest_batch(
            [packet("a", 2, source="改版")], batch_id="R2"
        )
        outcome = result.outcomes[0]
        self.assertEqual(outcome.outcome, REVIEW)
        self.assertEqual(outcome.reason, "content_changed")
        # 受理集合中仍只有 rev1。
        trace = self.svc.trace(location="R2 / #1")
        self.assertEqual(
            [v["revision"] for v in trace["accepted_versions"]], [1]
        )

    def test_review_records_explicit_revision_check(self):
        self.svc.ingest_batch([packet("a", 2)], batch_id="A1")
        # 收到 rev2 但修订未递增 → 仍进复核，证据标记为未通过。
        result = self.svc.ingest_batch([packet("a", 2, source="篡改")], batch_id="A2")
        self.assertEqual(result.outcomes[0].outcome, REVIEW)
        check = result.outcomes[0].detail["revision_check"]
        self.assertFalse(check["passed"])
        self.assertEqual(check["reason"], "revision_not_advanced")

    def test_review_admit_appends_without_overwriting(self):
        self.svc.ingest_batch([packet("a", 1, source="v1")], batch_id="D1")
        result = self.svc.ingest_batch(
            [packet("a", 2, source="v2")], batch_id="D2"
        )
        self.svc.resolve_review(result.outcomes[0].review_id,
                                decision="ADMIT", reason="核对一致")
        trace = self.svc.trace(location="D1 / #1")
        # rev1 事件仍在，rev2 以追加方式存在。
        self.assertEqual([v["revision"] for v in trace["accepted_versions"]], [1, 2])

    def test_review_reject_then_same_content_replays_rejection(self):
        self.svc.ingest_batch([packet("a", 1)], batch_id="J1")
        result = self.svc.ingest_batch([packet("a", 2, source="x")], batch_id="J2")
        review_id = result.outcomes[0].review_id
        self.svc.resolve_review(review_id, decision="REJECT", reason="来源不明")
        again = self.svc.ingest_batch([packet("a", 2, source="x")], batch_id="J3")
        self.assertEqual(again.outcomes[0].outcome, REPLAYED)


class SealedMaterialTest(IntakeTestCase):
    def _admit(self, record_id, revision, source, batch):
        result = self.svc.ingest_batch(
            [packet(record_id, revision, source=source)], batch_id=batch
        )
        if result.outcomes[0].outcome == REVIEW:
            self.svc.resolve_review(result.outcomes[0].review_id, decision="ADMIT")

    def test_sealed_revision_cannot_be_overwritten(self):
        self.svc.ingest_batch([packet("a", 1)], batch_id="S1")
        self._admit("a", 2, "v2", "S2")
        self.svc.seal_accepted("a", 2)
        attack = self.svc.ingest_batch(
            [packet("a", 2, source="伪造内容")], batch_id="S3"
        )
        self.assertEqual(attack.outcomes[0].outcome, QUARANTINED)
        self.assertEqual(attack.outcomes[0].reason, "sealed_revision_protected")

    def test_exact_resend_of_sealed_material_is_replayed(self):
        self.svc.ingest_batch([packet("a", 1)], batch_id="T1")
        self.svc.seal_accepted("a", 1)
        again = self.svc.ingest_batch([packet("a", 1)], batch_id="T2")
        self.assertEqual(again.outcomes[0].outcome, REPLAYED)

    def test_admit_review_against_newly_sealed_revision_refused(self):
        self.svc.ingest_batch([packet("a", 1)], batch_id="U1")
        opened = self.svc.ingest_batch(
            [packet("a", 2, source="候选")], batch_id="U2"
        )
        review_id = opened.outcomes[0].review_id
        # 另一条路径受理同内容 rev2 并签章（模拟复核期间线下签章）。
        self.svc.resolve_review(review_id, decision="ADMIT")
        self.svc.seal_accepted("a", 2)
        # 此时新内容 rev3 走复核，签章规则由 quarantine 路径覆盖；
        # 已处理复核不可二次更改：
        with self.assertRaises(ValueError):
            self.svc.resolve_review(review_id, decision="REJECT")


class RecoveryTest(IntakeTestCase):
    def _journal_lines(self):
        return [
            json.loads(line)
            for line in (self.tmp / "journal.jsonl").read_text("utf-8").splitlines()
        ]

    def test_notification_after_crash_is_sent_exactly_once(self):
        self.notifier.fail_times = 1
        result = self.svc.ingest_batch([packet("a", 1)], batch_id="C1")
        self.assertTrue(result.outcomes[0].detail["notification_pending"])
        # 重启恢复。
        self.reopen()
        report = self.svc.recover()
        self.assertEqual(len(report["delivered_notifications"]), 1)
        # 再恢复不重复。
        self.assertEqual(self.svc.recover()["delivered_notifications"], [])
        types = [e["type"] for e in self._journal_lines()]
        self.assertEqual(types.count("notification_delivered"), 1)
        self.assertEqual(types.count("record_accepted"), 1)

    def test_resume_archived_but_undecided_slot_does_not_duplicate(self):
        import hashlib

        good = packet("z", 1)
        bad = packet("z", 1, ts="2026-09-20T09:00:00")  # 无时区
        raw_dir = self.tmp / "raw" / "C9"
        raw_dir.mkdir(parents=True)
        (raw_dir / "0001.json").write_text(good, "utf-8")
        (raw_dir / "0002.json").write_text(bad, "utf-8")

        def sha(text):
            return hashlib.sha256(text.encode()).hexdigest()

        with (self.tmp / "journal.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "seq": 1, "event_id": "e1", "ts": "t", "type": "batch_registered",
                "batch_id": "C9", "item_count": 2}) + "\n")
            for slot, raw in ((1, good), (2, bad)):
                handle.write(json.dumps({
                    "seq": slot + 1, "event_id": f"e{slot+1}", "ts": "t",
                    "type": "raw_archived", "batch_id": "C9", "slot": slot,
                    "location": f"C9 / #{slot}",
                    "raw_path": str(raw_dir / f"{slot:04d}.json"),
                    "raw_sha256": sha(raw)}) + "\n")

        self.reopen()
        report = self.svc.recover()
        resumed = {r["location"]: r["outcome"] for r in report["resumed_slots"]}
        self.assertEqual(resumed, {"C9 / #1": ACCEPTED, "C9 / #2": QUARANTINED})
        # 第二次恢复为空，且没有重复归档/受理。
        self.assertEqual(self.svc.recover()["resumed_slots"], [])
        types = [e["type"] for e in self._journal_lines()]
        self.assertEqual(types.count("raw_archived"), 2)
        self.assertEqual(types.count("record_accepted"), 1)
        self.assertEqual(types.count("record_quarantined"), 1)
        self.assertEqual(types.count("notification_delivered"), 1)

    def test_repeating_same_batch_id_is_idempotent(self):
        items = [packet("a", 1), "{bad"]
        first = self.svc.ingest_batch(items, batch_id="BID")
        before = self._journal_lines()
        second = self.svc.ingest_batch(items, batch_id="BID")
        after = self._journal_lines()
        self.assertEqual([o.outcome for o in first.outcomes],
                         [o.outcome for o in second.outcomes])
        self.assertEqual(len(before), len(after))


class TraceabilityTest(IntakeTestCase):
    def test_trace_from_blocked_point_reaches_raw_and_conclusion(self):
        self.svc.ingest_batch(
            [packet("a", revision=-7)], batch_id="Q1"
        )
        trace = self.svc.trace(location="Q1 / #1")
        self.assertEqual(trace["conclusion"], "record_quarantined")
        self.assertEqual(trace["reason"], "revision_not_positive")
        self.assertEqual(trace["batch_id"], "Q1")
        self.assertTrue(Path(trace["raw_path"]).exists())
        self.assertEqual(trace["slot"], 1)
        self.assertIsInstance(trace["raw_sha256"], str)
        # 语义层拒绝也要留下记录身份：护士能看到是哪份清单的哪一版。
        self.assertEqual(trace["record_id"], "a")
        self.assertEqual(trace["revision"], -7)

    def test_contract_failure_keeps_claimed_identity_separately(self):
        # 合同层（无时区时间）失败时记录身份未经确认，
        # 只能以“申报值”留痕，不能冒充可信标识。
        self.svc.ingest_batch(
            [packet("申报-X", revision=-2, ts="2026-09-24T08:15:00")],
            batch_id="Q2",
        )
        trace = self.svc.trace(location="Q2 / #1")
        self.assertEqual(trace["reason"], "timezone_required")
        self.assertEqual(trace["claimed_record_id"], "申报-X")
        self.assertEqual(trace["claimed_revision"], -2)
        self.assertNotIn("record_id", trace)

    def test_trace_migrated_record_carries_chain(self):
        raw = packet("e", 1, ver=0, domain="referral",
                     ts="2026-09-20T09:00:00", timezone="+08:00")
        self.svc.ingest_batch([raw], batch_id="M1")
        trace = self.svc.trace(location="M1 / #1")
        self.assertEqual(trace["observed_version"], 0)
        self.assertEqual(trace["migration_chain"][0]["name"], "v0_to_v1")
        self.assertEqual(trace["conclusion"], "record_accepted")

    def test_trace_review_links_prior_location(self):
        self.svc.ingest_batch([packet("a", 1)], batch_id="P1")
        result = self.svc.ingest_batch([packet("a", 2, source="x")], batch_id="P2")
        review_id = result.outcomes[0].review_id
        trace = self.svc.trace(review_id=review_id)
        self.assertEqual(trace["review_open"], True)
        prior = [e for e in trace["event_chain"]
                 if e["type"] == "review_opened"][0]
        self.svc.resolve_review(review_id, decision="REJECT", reason="r")
        resolved = self.svc.trace(review_id=review_id)
        self.assertFalse(resolved["review_open"])
        self.assertEqual(resolved["review_resolution"]["decision"], "REJECT")

    def test_blocked_points_enumeration(self):
        self.svc.ingest_batch([
            "{bad",
            packet("f", 1, ver=2),
        ], batch_id="Z1")
        points = self.svc.blocked_points()
        kinds = {Path(p["raw_path"]).name: p["kind"] for p in points}
        self.assertEqual(kinds, {"0001.json": QUARANTINED, "0002.json": HELD_FUTURE})

    def test_replay_trace_reaches_original_location_and_resolution(self):
        self.svc.ingest_batch([packet("a", 1)], batch_id="V1")
        changed = self.svc.ingest_batch([packet("a", 2, source="x")], batch_id="V2")
        self.svc.resolve_review(changed.outcomes[0].review_id,
                                decision="REJECT", reason="来源不明")
        again = self.svc.ingest_batch([packet("a", 2, source="x")], batch_id="V3")
        self.assertEqual(again.outcomes[0].outcome, REPLAYED)
        trace = self.svc.trace(location="V3 / #1")
        self.assertEqual(trace["conclusion"], "record_replayed")
        prior = trace["replayed_prior"]
        self.assertEqual(prior["prior_location"], "V2 / #1")
        self.assertEqual(prior["prior_type"], "review_opened")
        self.assertEqual(prior["review_resolution"]["decision"], "REJECT")


if __name__ == "__main__":
    unittest.main()
