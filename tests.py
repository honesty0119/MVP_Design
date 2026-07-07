import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from agent import run_query_agent


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

    def test_qualification_explains_mql_blockers_and_readiness(self):
        detail = app.lead_detail(4)
        self.assertFalse(detail["qualification"]["can_mql"])
        self.assertIn("至少需要一条跟进记录", detail["qualification"]["blockers"])

        with app.db() as conn:
            ts = app.now_iso()
            conn.execute(
                "INSERT INTO follow_ups(lead_id, operator_id, content, created_at) VALUES (4, 1, ?, ?)",
                ("已确认需求，客户愿意继续沟通", ts),
            )
            conn.execute("UPDATE leads SET last_follow_up_at=?, updated_at=? WHERE id=4", (ts, ts))
        detail = app.lead_detail(4)
        self.assertTrue(detail["qualification"]["can_mql"])
        self.assertTrue(detail["qualification"]["suggested_mql"])
        self.assertGreaterEqual(detail["qualification"]["score"], 70)

    def test_sales_cannot_operate_other_owner_lead(self):
        lead_id, _ = app.create_lead({"phone": "13800007666", "source": "测试", "channel": "A"})
        with app.db() as conn:
            conn.execute("UPDATE leads SET owner_id=1 WHERE id=?", (lead_id,))
        with self.assertRaisesRegex(PermissionError, "销售只能操作自己负责的线索"):
            app.update_status(lead_id, {"status": "PENDING_CALL", "actor_id": 2})
        app.update_status(lead_id, {"status": "PENDING_CALL", "actor_id": 1})
        self.assertEqual(app.lead_detail(lead_id)["status"], "PENDING_CALL")

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

    def test_complete_new_to_sql_business_flow(self):
        lead_id, _ = app.create_lead({"phone": "13800005555", "source": "测试", "channel": "端到端"})
        with app.db() as conn:
            conn.execute("UPDATE leads SET owner_id=1 WHERE id=?", (lead_id,))
        app.update_status(lead_id, {"status": "PENDING_CALL"})
        app.process_call_callback(
            {"event_id": "call-e2e", "lead_id": lead_id, "result": "CONNECTED"}
        )
        app.update_status(lead_id, {"status": "VALID"})
        with app.db() as conn:
            ts = app.now_iso()
            conn.execute(
                "INSERT INTO follow_ups(lead_id, operator_id, content, created_at) VALUES (?, 1, ?, ?)",
                (lead_id, "已确认需求，下一步发送方案", ts),
            )
            conn.execute(
                "UPDATE leads SET last_follow_up_at=?, updated_at=? WHERE id=?",
                (ts, ts, lead_id),
            )
        app.update_status(lead_id, {"status": "MQL"})
        app.update_status(
            lead_id,
            {"status": "SQL", "opportunity_note": "客户确认预算及采购时间"},
        )
        detail = app.lead_detail(lead_id)
        self.assertEqual(detail["status"], "SQL")
        self.assertEqual(len(detail["follow_ups"]), 1)
        self.assertEqual(
            [item["to_status"] for item in reversed(detail["history"])],
            ["NEW", "PENDING_CALL", "CONNECTED", "VALID", "MQL", "SQL"],
        )

    def test_natural_language_query_explains_metric(self):
        result = app.natural_language_query("哪个渠道 SQL 转化率最高？")
        self.assertIn("SQL 转化率最高", result["answer"])
        self.assertIn("线索进入时间", result["definition"])

    def test_current_week_query_has_demo_data(self):
        result = app.execute_agent_tool(
            "channel_sql_conversion", {"period": "current_week"}
        )
        self.assertTrue(result["rows"])
        self.assertEqual(result["rows"][0]["channel"], "在线公开课")

    def test_llm_agent_selects_controlled_tool_then_answers(self):
        responses = iter(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "channel_sql_conversion",
                                            "arguments": '{"period":"current_week"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "本周在线公开课渠道的 SQL 转化率最高。",
                            }
                        }
                    ]
                },
            ]
        )

        def scripted_request(_payload):
            return next(responses)

        with patch.dict("os.environ", {"AGENT_LLM_MODE": "openai-compatible"}):
            result = run_query_agent(
                "本周哪个渠道 SQL 转化率最高？",
                app.execute_agent_tool,
                app.natural_language_query,
                request_fn=scripted_request,
            )
        self.assertEqual(result["agent"]["mode"], "llm")
        self.assertEqual(
            result["agent"]["tool_calls"][0]["name"],
            "channel_sql_conversion",
        )
        self.assertIn("服务端校验", [item["step"] for item in result["agent"]["trace"]])
        self.assertIn("Demo 使用当前状态快照", result["data_boundary"])
        self.assertIn("在线公开课", result["answer"])

    def test_agent_falls_back_without_api_key(self):
        with patch.dict(
            "os.environ",
            {
                "AGENT_LLM_MODE": "auto",
                "AGENT_LLM_API_KEY_ENV": "MISSING_TEST_KEY",
            },
            clear=False,
        ):
            result = run_query_agent(
                "当前有多少条线索跟进超时？",
                app.execute_agent_tool,
                app.natural_language_query,
            )
        self.assertEqual(result["agent"]["mode"], "rule-fallback")
        self.assertIn("超过 48 小时", result["answer"])


if __name__ == "__main__":
    unittest.main()
