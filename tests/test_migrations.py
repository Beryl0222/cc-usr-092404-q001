import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from referral_packet.errors import ContractError, FutureVersionError
from referral_packet.migrations import migrate_to_current, registered_versions
from referral_packet.parsing import parse_record_text

V0 = {
    "schema_version": 0,
    "record_id": "legacy-1",
    "domain": "referral",
    "occurred_at": "2026-09-19T17:30:00",
    "revision": 0,
    "source": "旧系统",
    "timezone": "+08:00",
}


class MigrationTest(unittest.TestCase):
    def test_v0_is_registered(self):
        self.assertIn(0, registered_versions())

    def test_v0_to_v1_rules(self):
        migrated, chain = migrate_to_current(dict(V0), 0)
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]["from_version"], 0)
        self.assertEqual(chain[0]["to_version"], 1)
        self.assertEqual(migrated["schema_version"], 1)
        self.assertEqual(migrated["domain"], "referral_packet")
        self.assertEqual(migrated["revision"], 1)
        # 时区只按显式声明附加，且保留在 ISO 串里。
        self.assertTrue(migrated["occurred_at"].endswith("+08:00"))

    def test_v0_without_timezone_declaration_is_refused(self):
        payload = dict(V0)
        del payload["timezone"]
        with self.assertRaises(ContractError) as ctx:
            migrate_to_current(payload, 0)
        self.assertEqual(ctx.exception.details["reason"], "missing_timezone_declaration")

    def test_v0_unknown_timezone_is_refused_not_guessed(self):
        payload = dict(V0, timezone="+09:00")
        with self.assertRaises(ContractError) as ctx:
            migrate_to_current(payload, 0)
        self.assertEqual(ctx.exception.details["reason"], "missing_timezone_declaration")

    def test_v0_unknown_domain_alias_refused(self):
        payload = dict(V0, domain="transfer")
        with self.assertRaises(ContractError) as ctx:
            migrate_to_current(payload, 0)
        self.assertEqual(ctx.exception.details["reason"], "unknown_legacy_domain")

    def test_negative_legacy_revision_cannot_be_washed(self):
        payload = dict(V0, revision=-1)
        with self.assertRaises(ContractError) as ctx:
            migrate_to_current(payload, 0)
        self.assertEqual(ctx.exception.details["reason"], "negative_legacy_revision")

    def test_legacy_read_end_to_end_is_traced_as_migrated(self):
        import json

        parsed = parse_record_text(json.dumps(V0, ensure_ascii=False))
        self.assertTrue(parsed.migrated)
        self.assertEqual(parsed.observed_version, 0)
        self.assertEqual(parsed.record.revision, 1)
        self.assertIsNotNone(parsed.record.occurred_at.tzinfo)

    def test_unregistered_older_version_is_refused(self):
        # schema_version=-1 不存在登记迁移：不得猜着读。
        import json

        text = json.dumps(dict(V0, schema_version=-1))
        with self.assertRaises(ContractError) as ctx:
            parse_record_text(text)
        self.assertEqual(ctx.exception.details["reason"], "unregistered_legacy_version")

    def test_future_version_is_never_migrated(self):
        payload = dict(V0, schema_version=9)
        with self.assertRaises(FutureVersionError):
            parse_record_text(__import__("json").dumps(payload))


if __name__ == "__main__":
    unittest.main()
