import os
import tempfile
import unittest
from pathlib import Path

import app


class BusinessRulesTest(unittest.TestCase):
    def setUp(self):
        self.original = app.DB_PATH
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.tmp.name) / "test.db"
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.original
        self.tmp.cleanup()

    def test_duplicate_phone_is_idempotent(self):
        first, created1 = app.create_lead({"phone": "13800009999", "source": "测试", "channel": "A"})
        second, created2 = app.create_lead({"phone": "13800009999", "source": "测试", "channel": "B"})
        self.assertEqual(first, second)
        self.assertTrue(created1)
        self.assertFalse(created2)

    def test_status_cannot_skip(self):
        lead_id, _ = app.create_lead({"phone": "13800008888", "source": "测试", "channel": "A"})
        with self.assertRaisesRegex(ValueError, "不允许"):
            app.update_status(lead_id, {"status": "SQL", "opportunity_note": "明确预算"})

    def test_invalid_requires_reason(self):
        lead_id, _ = app.create_lead({"phone": "13800007777", "source": "测试", "channel": "A"})
        with self.assertRaisesRegex(ValueError, "无效原因"):
            app.update_status(lead_id, {"status": "INVALID"})

    def test_call_callback_is_idempotent_and_cannot_roll_back(self):
        lead_id, _ = app.create_lead({"phone": "13800006666", "source": "测试", "channel": "A"})
        app.update_status(lead_id, {"status": "PENDING_CALL"})
        first = app.process_call_callback(
            {"event_id": "call-001", "lead_id": lead_id, "result": "CONNECTED"}
        )
        duplicate = app.process_call_callback(
            {"event_id": "call-001", "lead_id": lead_id, "result": "CONNECTED"}
        )
        self.assertTrue(first["status_changed"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(app.lead_detail(lead_id)["status"], "CONNECTED")


if __name__ == "__main__":
    unittest.main()
