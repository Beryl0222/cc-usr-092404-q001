import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet.errors import (
    ContractError,
    SemanticError,
    StructuralError,
    FutureVersionError,
)
from referral_packet.parsing import (
    parse_json_text,
    parse_payload,
    parse_record_text,
    validate_record_semantics,
    validate_revision_advances,
)
from referral_packet.contracts import DomainRecord

VALID = (
    '{"schema_version": 1, "record_id": "r-1", "domain": "referral_packet", '
    '"occurred_at": "2026-09-20T09:00:00+08:00", "revision": 1, "source": "x"}'
)


class StructureLayerTest(unittest.TestCase):
    def test_invalid_json_is_structural(self):
        with self.assertRaises(StructuralError) as ctx:
            parse_json_text("{not json")
        self.assertEqual(ctx.exception.details["reason"], "invalid_json")

    def test_top_level_array_is_structural(self):
        with self.assertRaises(StructuralError) as ctx:
            parse_json_text("[]")
        self.assertEqual(ctx.exception.details["reason"], "top_level_not_object")

    def test_top_level_scalar_is_structural(self):
        with self.assertRaises(StructuralError):
            parse_json_text('"a record"')


class ContractLayerTest(unittest.TestCase):
    def _payload(self, **overrides):
        payload = {
            "schema_version": 1,
            "record_id": "r-1",
            "domain": "referral_packet",
            "occurred_at": "2026-09-20T09:00:00+08:00",
            "revision": 1,
            "source": "x",
        }
        payload.update(overrides)
        return payload

    def test_missing_field(self):
        payload = self._payload()
        del payload["record_id"]
        with self.assertRaises(ContractError) as ctx:
            parse_payload(payload)
        self.assertEqual(ctx.exception.details["reason"], "missing_fields")

    def test_unknown_field_is_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            parse_payload(self._payload(triage_window="2026-09-20T09:30:00+08:00"))
        self.assertEqual(ctx.exception.details["reason"], "unknown_fields")

    def test_naive_timestamp_is_contract_error(self):
        # 事故根因之一：无时区时间无法判断接诊窗口。
        with self.assertRaises(ContractError) as ctx:
            parse_payload(self._payload(occurred_at="2026-09-20T09:00:00"))
        self.assertEqual(ctx.exception.details["reason"], "timezone_required")

    def test_bad_timestamp_shape(self):
        with self.assertRaises(ContractError) as ctx:
            parse_payload(self._payload(occurred_at="昨天上午"))
        self.assertEqual(ctx.exception.details["reason"], "bad_timestamp")

    def test_wrong_types(self):
        with self.assertRaises(ContractError):
            parse_payload(self._payload(revision="1"))
        with self.assertRaises(ContractError):
            parse_payload(self._payload(revision=True))  # bool 不得冒充整数
        with self.assertRaises(ContractError):
            parse_payload(self._payload(schema_version="1"))
        with self.assertRaises(ContractError):
            parse_payload(self._payload(record_id=""))
        with self.assertRaises(ContractError):
            parse_payload(self._payload(domain="Referral_Packet"))
        with self.assertRaises(ContractError):
            parse_payload(self._payload(source="   "))

    def test_future_version_raises_future_error(self):
        with self.assertRaises(FutureVersionError) as ctx:
            parse_payload(self._payload(schema_version=2))
        self.assertEqual(ctx.exception.details["observed_version"], 2)

    def test_error_layers_are_distinct(self):
        # 同一字段问题必须归到正确的层，不能笼统报错。
        self.assertIsInstance(_catch(parse_record_text, "{}"), ContractError)
        self.assertIsInstance(_catch(parse_record_text, "null"), StructuralError)


class SemanticLayerTest(unittest.TestCase):
    def _record(self, revision=1, domain="referral_packet"):
        from datetime import datetime, timezone, timedelta

        return DomainRecord(
            schema_version=1,
            record_id="r-1",
            domain=domain,
            occurred_at=datetime(2026, 9, 20, 9, 0, tzinfo=timezone(timedelta(hours=8))),
            revision=revision,
            source="x",
        )

    def test_negative_revision_is_semantic_error(self):
        with self.assertRaises(SemanticError) as ctx:
            validate_record_semantics(self._record(revision=-3))
        self.assertEqual(ctx.exception.details["reason"], "revision_not_positive")

    def test_zero_revision_is_semantic_error(self):
        with self.assertRaises(SemanticError):
            validate_record_semantics(self._record(revision=0))

    def test_unaccepted_domain_is_semantic_not_contract_error(self):
        # 形态合法（合同通过）但不是本院接收领域（语义拒绝）。
        with self.assertRaises(SemanticError) as ctx:
            validate_record_semantics(self._record(domain="discharge_note"))
        self.assertEqual(ctx.exception.details["reason"], "domain_not_accepted")

    def test_revision_must_advance(self):
        validate_revision_advances(self._record(revision=3), previous_revision=2)
        with self.assertRaises(SemanticError) as ctx:
            validate_revision_advances(self._record(revision=2), previous_revision=2)
        self.assertEqual(ctx.exception.details["reason"], "revision_not_advanced")
        with self.assertRaises(SemanticError):
            validate_revision_advances(self._record(revision=1), previous_revision=2)

    def test_full_entry_negative_revision_blocked(self):
        text = VALID.replace('"revision": 1', '"revision": -1')
        with self.assertRaises(SemanticError):
            parse_record_text(text)


def _catch(fn, *args):
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - 测试只检查异常类型
        return exc
    raise AssertionError("应当抛出异常")


if __name__ == "__main__":
    unittest.main()
