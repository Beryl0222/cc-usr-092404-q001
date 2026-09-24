"""数据合同测试：三层校验、版本路由与可追溯迁移。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet import (
    CURRENT_SCHEMA_VERSION,
    FieldContractError,
    FutureVersionError,
    JsonStructureError,
    SemanticContractError,
    load_record,
    parse_json_text,
    parse_record,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"

V1 = {
    "schema_version": 1,
    "record_id": "sample-019",
    "domain": "referral_packet",
    "occurred_at": "2026-09-20T09:00:00+08:00",
    "revision": 1,
    "source": "业务样例",
}


def v1_text(**overrides) -> str:
    payload = dict(V1)
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class CurrentContractTest(unittest.TestCase):
    def test_example_uses_current_contract(self):
        item = load_record(FIXTURES / "packet_manifest.json")
        self.assertEqual(item.domain, "referral_packet")
        self.assertGreater(item.revision, 0)
        self.assertIsNotNone(item.occurred_at.tzinfo)

    def test_z_suffix_is_accepted_as_utc(self):
        parsed = parse_json_text(v1_text(occurred_at="2026-09-20T01:00:00Z"))
        self.assertEqual(parsed.record.occurred_at.utcoffset().total_seconds(), 0)

    def test_native_v1_has_no_migration_trace(self):
        parsed = parse_json_text(json.dumps(V1))
        self.assertEqual(parsed.detected_version, CURRENT_SCHEMA_VERSION)
        self.assertIsNone(parsed.migration)


class JsonStructureLayerTest(unittest.TestCase):
    def test_malformed_json_is_structure_error(self):
        with self.assertRaises(JsonStructureError):
            parse_json_text('{"schema_version": 1, broken')

    def test_top_level_array_is_structure_error(self):
        with self.assertRaises(JsonStructureError):
            parse_json_text("[]")

    def test_top_level_scalar_is_structure_error(self):
        with self.assertRaises(JsonStructureError):
            parse_json_text('"just a string"')


class FieldContractLayerTest(unittest.TestCase):
    def test_missing_field(self):
        payload = dict(V1)
        del payload["revision"]
        with self.assertRaises(FieldContractError) as ctx:
            parse_record(payload)
        self.assertIn("revision", str(ctx.exception))

    def test_wrong_type_revision(self):
        with self.assertRaises(FieldContractError):
            parse_json_text(v1_text(revision="1"))

    def test_boolean_revision_rejected(self):
        with self.assertRaises(FieldContractError):
            parse_json_text(v1_text(revision=True))

    def test_unknown_field_rejected(self):
        payload = dict(V1, extra="x")
        with self.assertRaises(FieldContractError) as ctx:
            parse_record(payload)
        self.assertIn("extra", str(ctx.exception))

    def test_missing_schema_version(self):
        payload = dict(V1)
        del payload["schema_version"]
        with self.assertRaises(FieldContractError):
            parse_record(payload)

    def test_non_integer_schema_version(self):
        with self.assertRaises(FieldContractError):
            parse_json_text(v1_text(schema_version="1"))


class SemanticLayerTest(unittest.TestCase):
    def test_negative_revision_is_semantic_error(self):
        # 本次事故记录：负修订号不得再被当成正常资料。
        with self.assertRaises(SemanticContractError) as ctx:
            parse_json_text(v1_text(revision=-3))
        self.assertIn("revision", str(ctx.exception))

    def test_zero_revision_on_v1_is_semantic_error(self):
        with self.assertRaises(SemanticContractError):
            parse_json_text(v1_text(revision=0))

    def test_naive_datetime_is_semantic_error(self):
        # 本次事故记录：没有时区的时间无法判断接诊窗口。
        with self.assertRaises(SemanticContractError) as ctx:
            parse_json_text(v1_text(occurred_at="2026-09-20T09:00:00"))
        self.assertIn("时区", str(ctx.exception))

    def test_unparseable_datetime_is_semantic_error(self):
        with self.assertRaises(SemanticContractError):
            parse_json_text(v1_text(occurred_at="not-a-time"))

    def test_empty_record_id(self):
        with self.assertRaises(SemanticContractError):
            parse_json_text(v1_text(record_id="   "))

    def test_unknown_domain(self):
        with self.assertRaises(SemanticContractError) as ctx:
            parse_json_text(v1_text(domain="billing"))
        self.assertIn("billing", str(ctx.exception))

    def test_empty_source(self):
        with self.assertRaises(SemanticContractError):
            parse_json_text(v1_text(source=""))


class LegacyMigrationTest(unittest.TestCase):
    def test_v0_fixture_migrates_traceably(self):
        raw = (FIXTURES / "packet_manifest_legacy_v0.json").read_text(encoding="utf-8")
        parsed = parse_json_text(raw)

        self.assertEqual(parsed.detected_version, 0)
        self.assertEqual(parsed.record.schema_version, 1)

        trace = parsed.migration
        self.assertIsNotNone(trace)
        self.assertEqual((trace.from_version, trace.to_version), (0, 1))
        # 迁移假设必须显式可查：本地时区与修订号对齐。
        joined = " ".join(trace.assumptions + trace.steps)
        self.assertIn("+08:00", joined)
        self.assertIn("revision", joined)

        # v0 的无时区时间补上显式偏移；修订号从 0 起迁为从 1 起。
        self.assertEqual(
            parsed.record.occurred_at.utcoffset().total_seconds(), 8 * 3600
        )
        self.assertEqual(parsed.record.revision, 1)

        # 原始报文逐字保留，可追溯到迁移前内容。
        self.assertEqual(parsed.raw["occurred_at"], "2026-09-18T10:30:00")
        self.assertEqual(parsed.raw["revision"], 0)

    def test_v0_with_timezone_needs_no_timezone_assumption(self):
        raw = json.dumps(
            {
                "schema_version": 0,
                "record_id": "legacy-002",
                "domain": "referral_packet",
                "occurred_at": "2026-09-18T10:30:00+00:00",
                "revision": 2,
                "source": "旧版",
            }
        )
        parsed = parse_json_text(raw)
        self.assertEqual(parsed.record.revision, 3)
        self.assertNotIn("时区", " ".join(parsed.migration.assumptions))

    def test_v0_negative_revision_still_rejected(self):
        raw = json.dumps(
            {
                "schema_version": 0,
                "record_id": "legacy-bad",
                "domain": "referral_packet",
                "occurred_at": "2026-09-18T10:30:00",
                "revision": -1,
                "source": "旧版",
            }
        )
        with self.assertRaises(SemanticContractError):
            parse_json_text(raw)


class FutureVersionTest(unittest.TestCase):
    def test_future_version_keeps_raw_and_refuses_guessing(self):
        raw_text = (FIXTURES / "packet_manifest_future_v2.json").read_text(encoding="utf-8")
        with self.assertRaises(FutureVersionError) as ctx:
            parse_json_text(raw_text)
        self.assertEqual(ctx.exception.schema_version, 2)
        # 原文逐字保留，等待升级后重放。
        self.assertEqual(ctx.exception.raw["clinical_summary"]["triage"], "urgent")

    def test_future_version_never_returns_record(self):
        with self.assertRaises(FutureVersionError):
            parse_record({"schema_version": 99, "record_id": "x"})


if __name__ == "__main__":
    unittest.main()
