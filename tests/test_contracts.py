import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from referral_packet import load_record
from referral_packet.parsing import parse_record_text

FIXTURES = Path(__file__).parents[1] / "fixtures"


class ContractTest(unittest.TestCase):
    def test_example_uses_current_contract(self):
        item = load_record(FIXTURES / "packet_manifest.json")
        self.assertEqual(item.domain, "referral_packet")
        self.assertGreater(item.revision, 0)
        # 时间必须带时区，护士才能判断接诊窗口。
        self.assertIsNotNone(item.occurred_at.tzinfo)

    def test_legacy_fixture_migrates_to_current_contract(self):
        parsed = parse_record_text((FIXTURES / "legacy_packet_v0.json").read_text("utf-8"))
        self.assertEqual(parsed.observed_version, 0)
        self.assertEqual(len(parsed.migration_chain), 1)
        self.assertEqual(parsed.migration_chain[0]["name"], "v0_to_v1")
        record = parsed.record
        self.assertEqual(record.domain, "referral_packet")
        self.assertEqual(record.revision, 1)  # 旧版 0 起编，迁移显式 +1
        self.assertIsNotNone(record.occurred_at.tzinfo)

    def test_future_fixture_is_not_silently_read(self):
        from referral_packet import FutureVersionError

        with self.assertRaises(FutureVersionError):
            parse_record_text((FIXTURES / "future_packet_v2.json").read_text("utf-8"))


if __name__ == "__main__":
    unittest.main()
