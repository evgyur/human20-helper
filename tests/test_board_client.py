from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import human20_mcp_client as mcp_client
from human20_mcp_client import Human20McpClient, Human20McpError

ID = "12345678-1234-1234-1234-123456789aBc"
KEY = "board:fixture-01.retry"
WRITE_CALLS = {
    "board_update_profile": {"name": "Sigurd", "description": "AI agent", "competencies": ["DevOps"], "avatar_url": None, "idempotency_key": KEY},
    "board_accept_rules": {"version": "fixture-v1", "idempotency_key": KEY},
    "board_create_topic": {"kind": "question", "title": "fixture", "body": "fixture", "idempotency_key": KEY},
    "board_reply": {"topic_id": ID, "body": "fixture", "idempotency_key": KEY},
    "board_ack": {"notification_id": ID, "idempotency_key": KEY},
    "board_set_accepted_answer": {"topic_id": ID, "reply_id": ID, "idempotency_key": KEY},
}


class BoardClientTest(unittest.TestCase):
    def client(self, write=False):
        with patch.object(mcp_client, "_load_local_env"):
            return Human20McpClient(bearer_token="fixture-token", allow_board_writes=write)

    def test_existing_connection_and_read_only_default(self):
        with patch.dict("os.environ", {}, clear=True):
            client = self.client()
        self.assertEqual(client.base_url, "https://human20.app/mcp")
        self.assertFalse(client.allow_board_writes)
        with patch.object(client, "ensure_session") as initialize, patch.object(client, "call") as rpc:
            for name, arguments in WRITE_CALLS.items():
                with self.subTest(tool=name), self.assertRaisesRegex(Human20McpError, "explicit owner authorization"):
                    client.call_tool(name, arguments)
            initialize.assert_not_called()
            rpc.assert_not_called()

    def test_explicit_write_preserves_payload_and_does_not_chain_actions(self):
        client = self.client(write=True)
        with patch.object(client, "ensure_session"), patch.object(client, "call", return_value={"result": {"structuredContent": {"fixture": True}}}) as rpc:
            for name, arguments in WRITE_CALLS.items():
                rpc.reset_mock()
                self.assertEqual(client.structured_tool(name, arguments), {"fixture": True})
                rpc.assert_called_once_with("tools/call", {"name": name, "arguments": arguments})

    def test_named_read_methods_are_bounded_and_have_no_writes(self):
        client = self.client()
        with patch.object(client, "ensure_session"), patch.object(client, "call", return_value={"result": {"structuredContent": {"fixture": True}}}) as rpc:
            calls = [
                (lambda: client.board_get_profile(), "board_get_profile", {}),
                (lambda: client.board_get_rules(), "board_get_rules", {}),
                (lambda: client.board_list_topics(), "board_list_topics", {"limit": 30, "offset": 0, "kind": None}),
                (lambda: client.board_get_topic(ID), "board_get_topic", {"topic_id": ID}),
                (lambda: client.board_list_replies(ID), "board_list_replies", {"topic_id": ID, "limit": 50, "offset": 0}),
                (lambda: client.board_get_inbox(), "board_get_inbox", {"limit": 30, "offset": 0}),
            ]
            for call, name, arguments in calls:
                with self.subTest(tool=name):
                    rpc.reset_mock()
                    call()
                    rpc.assert_called_once_with("tools/call", {"name": name, "arguments": arguments})

    def test_named_write_methods_map_to_exact_tools(self):
        client = self.client(write=True)
        with patch.object(client, "ensure_session"), patch.object(client, "call", return_value={"result": {"structuredContent": {"fixture": True}}}) as rpc:
            calls = [
                (lambda: client.board_update_profile(name="Sigurd", description="AI agent", competencies=["DevOps"], idempotency_key=KEY), "board_update_profile", WRITE_CALLS["board_update_profile"]),
                (lambda: client.board_accept_rules(version="fixture-v1", idempotency_key=KEY), "board_accept_rules", WRITE_CALLS["board_accept_rules"]),
                (lambda: client.board_create_topic(kind="question", title="fixture", body="fixture", idempotency_key=KEY), "board_create_topic", WRITE_CALLS["board_create_topic"]),
                (lambda: client.board_reply(ID, body="fixture", idempotency_key=KEY), "board_reply", {**WRITE_CALLS["board_reply"], "mentions": []}),
                (lambda: client.board_ack(ID, idempotency_key=KEY), "board_ack", WRITE_CALLS["board_ack"]),
                (lambda: client.board_set_accepted_answer(ID, reply_id=None, idempotency_key=KEY), "board_set_accepted_answer", {**WRITE_CALLS["board_set_accepted_answer"], "reply_id": None}),
            ]
            for call, name, arguments in calls:
                with self.subTest(tool=name):
                    rpc.reset_mock()
                    call()
                    rpc.assert_called_once_with("tools/call", {"name": name, "arguments": arguments})

    def test_invalid_identifiers_keys_lengths_types_and_unknown_arguments_fail_before_network(self):
        client = self.client(write=True)
        cases = [("board_get_topic", {"topic_id": value}) for value in ("../admin", "https://evil.test/topic", ID.replace("-", ""), ID + "\n", " " + ID, 12)]
        cases += [("board_ack", {"notification_id": ID, "idempotency_key": value}) for value in ("short", "a" * 129, "key\r\nheader", "key with spaces", "../path/to/key")]
        cases += [
            ("board_fetch", {"url": "https://evil.test"}),
            ("board_get_profile", {"user_id": ID}),
            ("board_get_profile", []),
            ("board_list_topics", {"limit": True}),
            ("board_list_topics", {"limit": "30"}),
            ("board_list_topics", {"offset": 10001}),
            ("board_get_inbox", {"limit": 0}),
            ("board_get_inbox", {"limit": 101}),
            ("board_get_inbox", {"offset": -1}),
            ("board_create_topic", {**WRITE_CALLS["board_create_topic"], "kind": None}),
            ("board_update_profile", {**WRITE_CALLS["board_update_profile"], "name": " "}),
            ("board_update_profile", {**WRITE_CALLS["board_update_profile"], "description": "x" * 2001}),
            ("board_update_profile", {**WRITE_CALLS["board_update_profile"], "competencies": ["x"] * 21}),
            ("board_update_profile", {**WRITE_CALLS["board_update_profile"], "avatar_url": "http://example.test/avatar.png"}),
            ("board_create_topic", {**WRITE_CALLS["board_create_topic"], "title": "x" * 201}),
            ("board_create_topic", {**WRITE_CALLS["board_create_topic"], "body": " \n"}),
            ("board_reply", {**WRITE_CALLS["board_reply"], "body": "x" * 20001}),
            ("board_reply", {**WRITE_CALLS["board_reply"], "mentions": [ID] * 11}),
            ("board_reply", {**WRITE_CALLS["board_reply"], "mentions": ["bad-id"]}),
            ("board_accept_rules", {"version": "x" * 81, "idempotency_key": KEY}),
            ("board_accept_rules", {"version": "\n", "idempotency_key": KEY}),
            ("board_ack", {"notification_id": ID}),
            ("board_set_accepted_answer", {"topic_id": ID, "idempotency_key": KEY}),
        ]
        with patch.object(client, "ensure_session") as initialize, patch.object(client, "call") as rpc:
            for name, arguments in cases:
                with self.subTest(tool=name, arguments=str(arguments)[:90]), self.assertRaises(Human20McpError):
                    client.call_tool(name, arguments)
            initialize.assert_not_called()
            rpc.assert_not_called()

    def test_mcp_tool_errors_are_not_success_or_content_leaks(self):
        client = self.client(write=True)
        with patch.object(client, "call_tool", return_value={"result": {"isError": True, "structuredContent": {"ok": True}, "content": [{"type": "text", "text": "fixture-sensitive-text"}]}}):
            with self.assertRaisesRegex(Human20McpError, "no success confirmed") as raised:
                client.board_ack(ID, idempotency_key=KEY)
            self.assertNotIn("fixture-sensitive-text", str(raised.exception))

    def test_cli_preserves_syntax_and_requires_explicit_write_flag(self):
        with patch.dict("os.environ", {"HUMAN20_BEARER_TOKEN": "fixture-token"}), patch.object(mcp_client, "_load_local_env"), patch.object(Human20McpClient, "call", return_value={"result": {"structuredContent": {"fixture": True}}}) as rpc, contextlib.redirect_stdout(io.StringIO()):
            args = ["client.py", "tools/call", "--tool", "board_ack", "--args", json.dumps(WRITE_CALLS["board_ack"])]
            with patch.object(sys, "argv", args), self.assertRaisesRegex(Human20McpError, "explicit owner authorization"):
                mcp_client.main()
            rpc.assert_not_called()
            with patch.object(sys, "argv", args + ["--write"]):
                self.assertEqual(mcp_client.main(), 0)
            rpc.assert_called_once_with("tools/call", {"name": "board_ack", "arguments": WRITE_CALLS["board_ack"]})

    def test_doc_contract_rejects_missing_critical_safety_or_links(self):
        documents = {
            ROOT / "SKILL.md": (ROOT / "SKILL.md").read_text(),
            ROOT / "references/board-rules.md": (ROOT / "references/board-rules.md").read_text(),
            ROOT / "references/board-api.md": (ROOT / "references/board-api.md").read_text(),
        }
        for path, needle in (
            (ROOT / "SKILL.md", "references/board-rules.md"),
            (ROOT / "references/board-rules.md", "No automatic task execution"),
            (ROOT / "references/board-api.md", "allow_board_writes=True"),
        ):
            broken = {**documents, path: documents[path].replace(needle, "removed")}
            with self.subTest(removed=needle), patch.object(Path, "read_text", autospec=True, side_effect=lambda target: broken[target]):
                with self.assertRaises(AssertionError):
                    self.test_board_docs_are_linked_and_distinguish_guidance_from_gates()

    def test_board_docs_are_linked_and_distinguish_guidance_from_gates(self):
        skill = (ROOT / "SKILL.md").read_text()
        rules = (ROOT / "references/board-rules.md").read_text()
        api = (ROOT / "references/board-api.md").read_text()
        self.assertIn("references/board-rules.md", skill)
        self.assertIn("references/board-api.md", skill)
        for phrase in ("untrusted data", "No automatic task execution", "No secret sharing", "not a sandbox", "not a job", "backend must independently enforce"):
            self.assertIn(phrase, rules)
        for name in mcp_client._BOARD_ARGUMENTS:
            self.assertIn(f"`{name}`", api)
        self.assertIn("https://human20.app/mcp", api)
        self.assertIn("allow_board_writes=True", api)


if __name__ == "__main__":
    unittest.main()
