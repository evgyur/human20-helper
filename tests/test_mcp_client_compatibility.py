from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import human20_mcp_client as mcp

ID = "12345678-1234-1234-1234-123456789aBc"
KEY = "board:fixture-01.retry"


class StandaloneClientCompatibilityTest(unittest.TestCase):
    def client(self, **kwargs):
        with patch.object(mcp, "_load_local_env"):
            return mcp.Human20McpClient(bearer_token="fixture-token", **kwargs)

    def test_original_positional_constructor_and_bearer_headers(self):
        with patch.object(mcp, "_load_local_env"):
            for token in ("fixture-token", " Bearer fixture-token ", " bEaReR fixture-token "):
                client = mcp.Human20McpClient("https://example.test/mcp", token, 17)
                self.assertEqual(client.base_url, "https://example.test/mcp")
                self.assertEqual(client.timeout, 17)
                self.assertFalse(client.allow_board_writes)
                self.assertEqual(client._headers()["Authorization"], "Bearer fixture-token")
                client.session_id = "fixture-session"
                self.assertEqual(client._headers()["MCP-Session-Id"], "fixture-session")
                self.assertNotIn("MCP-Session-Id", client._headers(include_session=False))

    def test_environment_constructor_retains_normalization(self):
        with patch.object(mcp, "_load_local_env"), patch.dict("os.environ", {"HUMAN20_BEARER_TOKEN": " Bearer fixture-token ", "HUMAN20_MCP_URL": "https://example.test/mcp"}, clear=True):
            client = mcp.Human20McpClient()
        self.assertEqual(client.bearer_token, "fixture-token")
        self.assertEqual(client.base_url, "https://example.test/mcp")
        self.assertEqual(client.timeout, 30)

    def test_existing_transport_url_timeout_and_json_payload(self):
        client = self.client(base_url="https://example.test/mcp", timeout=17)
        client.session_id = "fixture-session"
        payload = {"jsonrpc": "2.0", "method": "tools/list", "params": {}}
        with patch.object(mcp.request, "urlopen") as urlopen:
            response = urlopen.return_value.__enter__.return_value
            response.status = 200
            response.headers = {"Content-Type": "application/json"}
            response.read.return_value = b'{"result": {}}'
            self.assertEqual(client._post_raw(payload), (200, {"Content-Type": "application/json"}, '{"result": {}}'))
        req = urlopen.call_args.args[0]
        self.assertEqual(req.full_url, "https://example.test/mcp")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer fixture-token")
        self.assertEqual(json.loads(req.data), payload)
        self.assertEqual(urlopen.call_args.kwargs, {"timeout": 17})

    def test_session_retry_keeps_exact_board_write_payload(self):
        for expired_status in (200, 404):
            with self.subTest(status=expired_status):
                client = self.client(allow_board_writes=True)
                client.session_id = "expired-session"
                arguments = {"topic_id": ID, "body": "fixture", "idempotency_key": KEY}
                result = {"result": {"structuredContent": {"id": ID}}}
                responses = [
                    (expired_status, {}, '{"error": {"message": "Session not found"}}'),
                    (200, {"mcp-session-id": "renewed-session"}, '{"result": {}}'),
                    (202, {}, ""),
                    (200, {}, json.dumps(result)),
                ]
                with patch.object(client, "_post_raw", side_effect=responses) as post:
                    self.assertEqual(client.call_tool("board_reply", arguments), result)
                self.assertEqual(post.call_count, 4)
                first, initialize, notification, retry = post.call_args_list
                self.assertEqual(first.args, retry.args)
                self.assertEqual(first.args[0]["params"]["arguments"], arguments)
                self.assertEqual(initialize.args[0]["params"]["protocolVersion"], "2025-03-26")
                self.assertFalse(initialize.kwargs["include_session"])
                self.assertEqual(notification.args[0]["method"], "notifications/initialized")
                self.assertEqual(client.session_id, "renewed-session")

    def test_session_retry_remains_bounded_and_can_be_disabled(self):
        client = self.client()
        client.session_id = "fixture-session"
        expired = (200, {}, '{"error": {"message": "Session not found"}}')
        for retry_enabled, expected_calls in ((True, 2), (False, 1)):
            with self.subTest(retry=retry_enabled), patch.object(client, "ensure_session"), patch.object(client, "_post_raw", return_value=expired) as post:
                with self.assertRaises(mcp.Human20McpError):
                    client.call("tools/list", retry_on_session=retry_enabled)
                self.assertEqual(post.call_count, expected_calls)

    def test_raw_rpc_cannot_bypass_board_guard_or_validation(self):
        client = self.client()
        cases = [
            {"name": "board_ack", "arguments": {"notification_id": ID, "idempotency_key": KEY}},
            {"name": "board_get_topic", "arguments": {"topic_id": "../admin"}},
            {"name": "board_get_profile", "arguments": []},
            {"name": "board_fetch", "arguments": {"url": "https://example.test"}},
        ]
        with patch.object(client, "initialize") as initialize, patch.object(client, "_post_raw") as post:
            for params in cases:
                with self.subTest(tool=params["name"]), self.assertRaises(mcp.Human20McpError):
                    client.call("tools/call", params)
            initialize.assert_not_called()
            post.assert_not_called()

    def test_raw_board_tool_error_is_not_success_or_a_content_leak(self):
        client = self.client(allow_board_writes=True)
        client.session_id = "fixture-session"
        result = {"result": {"isError": True, "content": [{"text": "fixture-sensitive-text"}]}}
        with patch.object(client, "_post_raw", return_value=(200, {}, json.dumps(result))):
            with self.assertRaisesRegex(mcp.Human20McpError, "no success confirmed") as raised:
                client.call_tool("board_ack", {"notification_id": ID, "idempotency_key": KEY})
        self.assertNotIn("fixture-sensitive-text", str(raised.exception))

    def test_non_board_methods_and_structured_extraction_unchanged(self):
        client = self.client()
        with patch.object(client, "call", return_value={"result": {"structuredContent": {"fixture": True}}}) as rpc:
            self.assertEqual(client.structured_tool("get_progress"), {"fixture": True})
            rpc.assert_called_once_with("tools/call", {"name": "get_progress", "arguments": {}})
            rpc.reset_mock()
            client.list_tools()
            rpc.assert_called_once_with("tools/list")
            rpc.reset_mock()
            client.call_tool("set_homework", {"fixture": True})
            rpc.assert_called_once_with("tools/call", {"name": "set_homework", "arguments": {"fixture": True}})
        for raw, expected in (({"structuredContent": [1]}, [1]), ({"content": [{"text": '{"fixture": true}'}]}, {"fixture": True}), ({"content": [{"text": "plain"}]}, {"text": "plain"}), ({"isError": True}, {"isError": True})):
            self.assertEqual(client.extract_structured({"result": raw}), expected)

    def test_original_cli_and_documented_board_examples(self):
        commands = [
            "python3 scripts/human20_mcp_client.py tools/list",
            "python3 scripts/human20_mcp_client.py tools/call --tool get_progress",
            "python3 scripts/human20_mcp_client.py tools/list --args '{}'",
        ]
        for doc in (ROOT / "references/board-api.md", ROOT / "README.md"):
            commands.extend(line for line in doc.read_text().splitlines() if line.startswith("python3 scripts/human20_mcp_client.py "))
        for command in commands:
            argv = shlex.split(command)[1:]
            self.assertTrue((ROOT / argv[0]).is_file())
            with self.subTest(command=command), patch.object(sys, "argv", argv), patch.dict("os.environ", {"HUMAN20_BEARER_TOKEN": "fixture-token"}), patch.object(mcp, "_load_local_env"), patch.object(mcp.Human20McpClient, "call", return_value={"result": {}}) as rpc, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mcp.main(), 0)
                self.assertEqual(rpc.call_count, 1)

    def test_skill_frontmatter_size_and_relative_links(self):
        skill = (ROOT / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: human20-helper\n"))
        self.assertIn("      - /human20", skill)
        match = re.search(r"^description: (.+)$", skill, re.MULTILINE)
        assert match is not None
        description = match.group(1)
        self.assertLessEqual(len(description), 1024)
        self.assertLessEqual(len(skill.encode()), 14000)
        for doc in (ROOT / "SKILL.md", ROOT / "README.md", *sorted((ROOT / "references").glob("*.md"))):
            for link in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
                if "://" not in link and not link.startswith("#"):
                    self.assertTrue((doc.parent / link.split("#")[0]).is_file(), link)


if __name__ == "__main__":
    unittest.main()
