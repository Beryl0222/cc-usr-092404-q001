"""收件流程测试：隔离、顺序、幂等、复核、签章保护与停机恢复。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet import (
    ACCEPTED,
    DUPLICATE,
    HELD_FUTURE_VERSION,
    QUARANTINED,
    REVIEW,
    STAGE_NOTIFIED,
    STAGE_PERSISTED,
    InMemoryNotifier,
    Inbox,
    IncomingItem,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


def make_raw(record_id: str, revision: int = 1, **overrides) -> str:
    payload = {
        "schema_version": 1,
        "record_id": record_id,
        "domain": "referral_packet",
        "occurred_at": "2026-09-20T09:00:00+08:00",
        "revision": revision,
        "source": "乡镇卫生院",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def item(position, record_id: str, revision: int = 1, **overrides) -> IncomingItem:
    return IncomingItem(position, make_raw(record_id, revision, **overrides))


class BatchIsolationTest(unittest.TestCase):
    def test_bad_record_only_quarantines_itself_and_order_is_kept(self):
        inbox = Inbox()
        items = [
            item("A", "rec-1"),
            IncomingItem("B", make_raw("rec-bad", revision=-2)),  # 事故同款：负修订
            IncomingItem("C", make_raw("rec-c", occurred_at="2026-09-20T09:00:00")),  # 无时区
            item("D", "rec-4"),
            IncomingItem("E", "{not json"),
            item("F", "rec-6"),
        ]
        result = inbox.receive_batch(items)

        self.assertIsNone(result.outage)
        self.assertEqual(
            [r.status for r in result.receipts],
            [ACCEPTED, QUARANTINED, QUARANTINED, ACCEPTED, QUARANTINED, ACCEPTED],
        )
        # 回执严格按输入顺序，合法记录的处理顺序与输入一致。
        self.assertEqual([r.position for r in result.receipts], list("ABCDEF"))
        self.assertEqual(
            inbox.notifier.sent,
            [("rec-1", 1), ("rec-4", 1), ("rec-6", 1)],
        )

        # 每个隔离点都能追到原始位置、失败层与原因，原文保留。
        bad_b = inbox.registry.blocked["B"]
        self.assertEqual(bad_b.kind, QUARANTINED)
        self.assertEqual(bad_b.layer, "semantic")
        self.assertIn("revision", bad_b.reason)
        self.assertEqual(json.loads(bad_b.raw)["revision"], -2)

        bad_c = inbox.registry.blocked["C"]
        self.assertEqual(bad_c.layer, "semantic")
        self.assertIn("时区", bad_c.reason)

        bad_e = inbox.registry.blocked["E"]
        self.assertEqual(bad_e.layer, "json")
        self.assertEqual(bad_e.raw, "{not json")

        # 合法记录全部到达 notified 阶段。
        for receipt in result.accepted:
            self.assertEqual(receipt.stage, STAGE_NOTIFIED)

    def test_field_layer_error_is_quarantined_with_layer(self):
        inbox = Inbox()
        result = inbox.receive_batch(
            [IncomingItem("P1", make_raw("rec-x", revision="one"))]
        )
        self.assertEqual(result.receipts[0].status, QUARANTINED)
        self.assertEqual(inbox.registry.blocked["P1"].layer, "field")


class FutureVersionHoldTest(unittest.TestCase):
    def test_future_version_is_held_with_raw_and_does_not_block_batch(self):
        raw_future = (FIXTURES / "packet_manifest_future_v2.json").read_text(
            encoding="utf-8"
        )
        inbox = Inbox()
        result = inbox.receive_batch(
            [IncomingItem("F1", raw_future), item("F2", "rec-ok")]
        )
        self.assertEqual(
            [r.status for r in result.receipts], [HELD_FUTURE_VERSION, ACCEPTED]
        )
        held = inbox.registry.blocked["F1"]
        self.assertEqual(held.kind, HELD_FUTURE_VERSION)
        self.assertEqual(held.detected_version, 2)
        # 原文逐字保留等待升级，未做任何猜测解释。
        self.assertEqual(held.raw, raw_future)
        self.assertEqual(inbox.notifier.sent, [("rec-ok", 1)])


class DuplicateAndReviewTest(unittest.TestCase):
    def test_exact_retransmit_returns_existing_result(self):
        inbox = Inbox()
        first = inbox.receive_batch([item("P1", "rec-1")])
        self.assertEqual(first.receipts[0].status, ACCEPTED)

        second = inbox.receive_batch([item("P2", "rec-1")])
        receipt = second.receipts[0]
        self.assertEqual(receipt.status, DUPLICATE)
        self.assertEqual(receipt.duplicate_of, "P1")
        self.assertEqual(receipt.stage, STAGE_NOTIFIED)

        # 不重复落库、不重复通知。
        self.assertEqual(inbox.notifier.sent, [("rec-1", 1)])
        self.assertEqual(len(inbox.registry.entries), 1)

    def test_retransmit_with_reordered_fields_is_still_duplicate(self):
        inbox = Inbox()
        inbox.receive_batch([item("P1", "rec-1")])
        reordered = json.dumps(
            {
                "source": "乡镇卫生院",
                "revision": 1,
                "occurred_at": "2026-09-20T09:00:00+08:00",
                "domain": "referral_packet",
                "record_id": "rec-1",
                "schema_version": 1,
            },
            ensure_ascii=False,
        )
        result = inbox.receive_batch([IncomingItem("P2", reordered)])
        self.assertEqual(result.receipts[0].status, DUPLICATE)

    def test_changed_content_goes_to_review_and_never_overwrites(self):
        inbox = Inbox()
        inbox.receive_batch([item("P1", "rec-1", source="首诊记录")])
        original = inbox.registry.entries["rec-1"].record

        result = inbox.receive_batch(
            [item("P2", "rec-1", revision=2, source="改写后的记录")]
        )
        receipt = result.receipts[0]
        self.assertEqual(receipt.status, REVIEW)
        self.assertFalse(receipt.sealed_protected)

        # 既有材料原样保留，复核区保留变化后的原文。
        self.assertIs(inbox.registry.entries["rec-1"].record, original)
        self.assertEqual(inbox.registry.entries["rec-1"].record.source, "首诊记录")
        review = inbox.registry.blocked["P2"]
        self.assertEqual(review.kind, REVIEW)
        self.assertEqual(review.record_id, "rec-1")
        self.assertEqual(review.existing_revision, 1)
        self.assertIn("改写后的记录", review.raw)
        self.assertEqual(inbox.notifier.sent, [("rec-1", 1)])

    def test_sealed_material_cannot_be_overwritten(self):
        inbox = Inbox()
        inbox.receive_batch([item("P1", "rec-1")])
        inbox.registry.seal("rec-1")

        result = inbox.receive_batch([item("P2", "rec-1", revision=2)])
        receipt = result.receipts[0]
        self.assertEqual(receipt.status, REVIEW)
        self.assertTrue(receipt.sealed_protected)

        entry = inbox.registry.entries["rec-1"]
        self.assertEqual(entry.record.revision, 1)  # 签章材料未被覆盖
        self.assertTrue(entry.sealed)
        self.assertTrue(inbox.registry.blocked["P2"].sealed_protected)

    def test_sealed_material_exact_retransmit_still_returns_existing(self):
        inbox = Inbox()
        inbox.receive_batch([item("P1", "rec-1")])
        inbox.registry.seal("rec-1")
        result = inbox.receive_batch([item("P2", "rec-1")])
        self.assertEqual(result.receipts[0].status, DUPLICATE)


class LegacyIntakeTest(unittest.TestCase):
    def test_legacy_v0_is_accepted_with_migration_trace(self):
        raw_legacy = (FIXTURES / "packet_manifest_legacy_v0.json").read_text(
            encoding="utf-8"
        )
        inbox = Inbox()
        result = inbox.receive_batch([IncomingItem("L1", raw_legacy)])

        receipt = result.receipts[0]
        self.assertEqual(receipt.status, ACCEPTED)
        self.assertEqual(receipt.detected_version, 0)
        self.assertIsNotNone(receipt_migration := receipt.migration)
        self.assertEqual(
            (receipt_migration.from_version, receipt_migration.to_version), (0, 1)
        )
        self.assertEqual(receipt.revision, 1)  # v0 rev0 → v1 rev1
        self.assertEqual(inbox.notifier.sent, [("sample-legacy-001", 1)])

        # 追溯：从回执到迁移假设与原文。
        trace = inbox.trace("L1")
        self.assertIn("+08:00", " ".join(trace.migration.assumptions))


class OutageRecoveryTest(unittest.TestCase):
    def _run_outage_scenario(self):
        inbox = Inbox()
        crashes = []

        def crash_once(record):
            if not crashes:
                crashes.append(record.record_id)
                return  # 首次落库后返回到 _accept_new，随即抛出停机
            raise AssertionError("停机钩子只应触发一次")

        inbox.outage = crash_once
        items = [
            item("N1", "rec-1"),
            item("N2", "rec-2"),
            item("N3", "rec-3"),
        ]
        result = inbox.receive_batch(items)
        return inbox, result, items

    def test_outage_between_persist_and_notify(self):
        inbox, result, _ = self._run_outage_scenario()

        self.assertIsNotNone(result.outage)
        # 第一条已落库但通知未发出；批次中止，N3 未处理。
        self.assertEqual([r.position for r in result.receipts], ["N1"])
        self.assertEqual(result.receipts[0].stage, STAGE_PERSISTED)
        self.assertEqual(result.resume_position, "N2")
        self.assertEqual(inbox.notifier.sent, [])
        self.assertEqual(
            inbox.registry.entries["rec-1"].stage, STAGE_PERSISTED
        )

    def test_recovery_only_redoes_unfinished_actions(self):
        inbox, result, items = self._run_outage_scenario()
        inbox.outage = None  # 系统恢复

        recoveries = inbox.recover()
        self.assertEqual(len(recoveries), 1)
        recovery = recoveries[0]
        self.assertTrue(recovery.recovered)
        self.assertEqual(recovery.record_id, "rec-1")
        self.assertEqual(recovery.stage, STAGE_NOTIFIED)

        # 只补做了通知：通知恰好一次，落库记录未被重建。
        self.assertEqual(inbox.notifier.sent, [("rec-1", 1)])
        self.assertEqual(inbox.registry.entries["rec-1"].stage, STAGE_NOTIFIED)

        # 恢复是幂等的：再次恢复不再产生任何动作。
        self.assertEqual(inbox.recover(), ())
        self.assertEqual(inbox.notifier.sent, [("rec-1", 1)])

        # 恢复后从断点继续收件：N2/N3 正常处理。
        rest = inbox.receive_batch(items[1:])
        self.assertEqual([r.status for r in rest.receipts], [ACCEPTED, ACCEPTED])
        self.assertEqual(
            inbox.notifier.sent, [("rec-1", 1), ("rec-2", 1), ("rec-3", 1)]
        )

    def test_every_blocking_point_is_traceable(self):
        inbox, result, items = self._run_outage_scenario()
        inbox.outage = None
        inbox.recover()
        inbox.receive_batch(
            [
                items[1],
                IncomingItem("N4", make_raw("rec-bad", revision=-1)),
                items[2],
                item("N5", "rec-1"),  # 完全重传
            ]
        )

        # 每个位置都能追到处理结论。
        self.assertEqual(inbox.trace("N1").status, ACCEPTED)
        self.assertTrue(inbox.trace("N1").recovered)
        self.assertEqual(inbox.trace("N2").status, ACCEPTED)
        self.assertEqual(inbox.trace("N3").status, ACCEPTED)
        self.assertEqual(inbox.trace("N4").status, QUARANTINED)
        self.assertEqual(inbox.trace("N5").status, DUPLICATE)
        self.assertEqual(inbox.trace("N5").duplicate_of, "N1")

        # 阻塞点能追到原文与原因。
        blocked = inbox.registry.blocked["N4"]
        self.assertEqual(blocked.position, "N4")
        self.assertIn("revision", blocked.reason)


if __name__ == "__main__":
    unittest.main()
