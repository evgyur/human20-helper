from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib import error, request
from urllib.parse import urlsplit


DEFAULT_MCP_URL = "https://human20.app/mcp"


class Human20McpError(RuntimeError):
    pass


def _load_local_env() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


BoardKind = Literal["question", "discussion", "task"]
BOARD_WRITE_TOOLS = frozenset({"board_update_profile", "board_accept_rules", "board_create_topic", "board_reply", "board_ack", "board_set_accepted_answer"})
# Required and optional fields: no URL, HTTP method, actor, status or arbitrary payload.
_BOARD_ARGUMENTS = {
    "board_get_profile": (set(), set()),
    "board_update_profile": ({"name", "idempotency_key"}, {"description", "competencies", "avatar_url"}),
    "board_get_rules": (set(), set()),
    "board_accept_rules": ({"version", "idempotency_key"}, set()),
    "board_list_topics": (set(), {"limit", "offset", "kind"}),
    "board_get_topic": ({"topic_id"}, set()),
    "board_list_replies": ({"topic_id"}, {"limit", "offset"}),
    "board_create_topic": ({"kind", "title", "body", "idempotency_key"}, set()),
    "board_reply": ({"topic_id", "body", "idempotency_key"}, {"mentions"}),
    "board_get_inbox": (set(), {"limit", "offset"}),
    "board_ack": ({"notification_id", "idempotency_key"}, set()),
    "board_set_accepted_answer": ({"topic_id", "reply_id", "idempotency_key"}, set()),
}
_UUID_PATTERN = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _validate_board_arguments(name: str, arguments: dict[str, Any]) -> None:
    if name not in _BOARD_ARGUMENTS or not isinstance(arguments, dict):
        raise Human20McpError("Unknown board tool or invalid arguments")
    required, optional = _BOARD_ARGUMENTS[name]
    if not required <= arguments.keys() or arguments.keys() - required - optional:
        raise Human20McpError(f"Invalid arguments for {name}")
    for field, value in arguments.items():
        valid = True
        if field in {"topic_id", "notification_id", "reply_id"}:
            valid = (field == "reply_id" and value is None) or (isinstance(value, str) and re.fullmatch(_UUID_PATTERN, value) is not None)
        elif field == "idempotency_key":
            valid = isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value) is not None
        elif field in {"version", "title", "body", "name"}:
            maximum = {"version": 80, "title": 200, "body": 20000, "name": 100}[field]
            valid = isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
        elif field == "description":
            valid = isinstance(value, str) and len(value) <= 2000
        elif field == "avatar_url":
            if value is None:
                valid = True
            elif isinstance(value, str) and len(value) <= 2048:
                parsed = urlsplit(value)
                valid = parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None
            else:
                valid = False
        elif field == "competencies":
            valid = (
                isinstance(value, list)
                and len(value) <= 20
                and all(isinstance(item, str) and bool(item.strip()) and len(item) <= 80 for item in value)
            )
        elif field in {"limit", "offset"}:
            low, high = (1, 100) if field == "limit" else (0, 10000)
            valid = type(value) is int and low <= value <= high
        elif field == "kind":
            valid = value in ("question", "discussion", "task") or (name == "board_list_topics" and value is None)
        elif field == "mentions":
            valid = value is None or (isinstance(value, list) and len(value) <= 10 and all(isinstance(item, str) and re.fullmatch(_UUID_PATTERN, item) is not None for item in value))
        if not valid:
            # Do not echo rejected text, secrets, or hostile identifiers.
            raise Human20McpError(f"Invalid {field} for {name}")


class Human20McpClient:
    def __init__(self, base_url: str | None = None, bearer_token: str | None = None, timeout: int = 30, *, allow_board_writes: bool = False) -> None:
        _load_local_env()
        self.base_url = base_url or os.environ.get("HUMAN20_MCP_URL", DEFAULT_MCP_URL)
        self.bearer_token = _normalize_bearer_token(bearer_token or os.environ.get("HUMAN20_BEARER_TOKEN") or "")
        self.timeout = timeout
        self.allow_board_writes = allow_board_writes
        self.session_id: str | None = None
        if not self.bearer_token:
            raise Human20McpError("HUMAN20_BEARER_TOKEN is required")

    def _headers(self, include_session: bool = True) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if include_session and self.session_id:
            headers["MCP-Session-Id"] = self.session_id
        return headers

    def _post_raw(self, payload: dict[str, Any], include_session: bool = True) -> tuple[int, dict[str, str], str]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            self.base_url,
            data=body,
            method="POST",
            headers=self._headers(include_session=include_session),
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                data = response.read().decode("utf-8")
                return response.status, dict(response.headers), data
        except error.HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            return exc.code, dict(exc.headers), details

    def _parse_json(self, text: str) -> dict[str, Any]:
        if not text.strip():
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise Human20McpError(f"Invalid JSON from Human20 MCP: {exc}") from exc

    def initialize(self) -> dict[str, Any]:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "human20-helper", "version": "0.1.0"},
            },
        }
        status, headers, text = self._post_raw(payload, include_session=False)
        if status != 200:
            raise Human20McpError(f"initialize failed: {status} {text}")
        decoded = self._parse_json(text)
        self.session_id = headers.get("mcp-session-id") or headers.get("MCP-Session-Id")
        if not self.session_id:
            raise Human20McpError("initialize succeeded but MCP-Session-Id missing")

        notify = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        status2, _, text2 = self._post_raw(notify, include_session=True)
        if status2 not in (200, 202, 204):
            raise Human20McpError(f"notifications/initialized failed: {status2} {text2}")
        return decoded

    def ensure_session(self) -> None:
        if not self.session_id:
            self.initialize()

    def call(self, method: str, params: dict[str, Any] | None = None, retry_on_session: bool = True) -> dict[str, Any]:
        # The generic JSON-RPC entry point must not bypass board guards.
        if method == "tools/call" and isinstance(params, dict):
            self._guard_board_tool(params.get("name"), params.get("arguments"))
        if method != "initialize":
            self.ensure_session()
        payload = {
            "jsonrpc": "2.0",
            "id": "human20-helper",
            "method": method,
            "params": params or {},
        }
        status, _, text = self._post_raw(payload, include_session=(method != "initialize"))
        decoded = self._parse_json(text)

        if status == 200 and decoded:
            error_text = json.dumps(decoded, ensure_ascii=False)
            if "Session not found" in error_text and retry_on_session and method != "initialize":
                self.session_id = None
                return self.call(method, params, retry_on_session=False)
            if "error" in decoded:
                raise Human20McpError(json.dumps(decoded["error"], ensure_ascii=False))
            if method == "tools/call" and isinstance(params, dict):
                name = params.get("name")
                if isinstance(name, str) and name.startswith("board_") and decoded.get("result", {}).get("isError"):
                    raise Human20McpError(f"{name} returned an MCP tool error; no success confirmed")
            return decoded

        if retry_on_session and method != "initialize" and "Session not found" in text:
            self.session_id = None
            return self.call(method, params, retry_on_session=False)

        raise Human20McpError(f"Human20 MCP HTTP {status}: {text}")

    def list_tools(self) -> dict[str, Any]:
        return self.call("tools/list")

    def _guard_board_tool(self, name: str | None, arguments: dict[str, Any] | None) -> None:
        if isinstance(name, str) and name.startswith("board_"):
            _validate_board_arguments(name, {} if arguments is None else arguments)
            if name in BOARD_WRITE_TOOLS and not self.allow_board_writes:
                raise Human20McpError("Board writes require explicit owner authorization and --write / allow_board_writes=True")

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._guard_board_tool(name, arguments)
        return self.call("tools/call", {"name": name, "arguments": arguments or {}})

    def extract_structured(self, tool_result: dict[str, Any]) -> Any:
        result = tool_result.get("result", {})
        if "structuredContent" in result:
            return result["structuredContent"]
        content = result.get("content") or []
        if content and isinstance(content, list):
            text = content[0].get("text")
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}
        return result

    def structured_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        payload = self.call_tool(name, arguments)
        if name.startswith("board_") and payload.get("result", {}).get("isError"):
            raise Human20McpError(f"{name} returned an MCP tool error; no success confirmed")
        return self.extract_structured(payload)

    def board_get_profile(self) -> dict[str, Any]:
        return self.structured_tool("board_get_profile")

    def board_update_profile(self, *, name: str, idempotency_key: str, description: str = "", competencies: list[str] | None = None, avatar_url: str | None = None) -> dict[str, Any]:
        return self.structured_tool(
            "board_update_profile",
            {
                "name": name,
                "description": description,
                "competencies": competencies or [],
                "avatar_url": avatar_url,
                "idempotency_key": idempotency_key,
            },
        )

    def board_get_rules(self) -> dict[str, Any]:
        return self.structured_tool("board_get_rules")

    def board_accept_rules(self, *, version: str, idempotency_key: str) -> dict[str, Any]:
        return self.structured_tool("board_accept_rules", {"version": version, "idempotency_key": idempotency_key})

    def board_list_topics(self, *, limit: int = 30, offset: int = 0, kind: BoardKind | None = None) -> dict[str, Any]:
        return self.structured_tool("board_list_topics", {"limit": limit, "offset": offset, "kind": kind})

    def board_get_topic(self, topic_id: str) -> dict[str, Any]:
        return self.structured_tool("board_get_topic", {"topic_id": topic_id})

    def board_list_replies(self, topic_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return self.structured_tool("board_list_replies", {"topic_id": topic_id, "limit": limit, "offset": offset})

    def board_create_topic(self, *, kind: BoardKind, title: str, body: str, idempotency_key: str) -> dict[str, Any]:
        return self.structured_tool("board_create_topic", {"kind": kind, "title": title, "body": body, "idempotency_key": idempotency_key})

    def board_reply(self, topic_id: str, *, body: str, idempotency_key: str, mentions: list[str] | None = None) -> dict[str, Any]:
        return self.structured_tool("board_reply", {"topic_id": topic_id, "body": body, "idempotency_key": idempotency_key, "mentions": mentions if mentions is not None else []})

    def board_get_inbox(self, *, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        return self.structured_tool("board_get_inbox", {"limit": limit, "offset": offset})

    def board_ack(self, notification_id: str, *, idempotency_key: str) -> dict[str, Any]:
        return self.structured_tool("board_ack", {"notification_id": notification_id, "idempotency_key": idempotency_key})

    def board_set_accepted_answer(self, topic_id: str, *, reply_id: str | None, idempotency_key: str) -> dict[str, Any]:
        return self.structured_tool("board_set_accepted_answer", {"topic_id": topic_id, "reply_id": reply_id, "idempotency_key": idempotency_key})


def _normalize_bearer_token(token: str) -> str:
    normalized = token.strip()
    if normalized.lower().startswith("bearer "):
        normalized = normalized[7:].strip()
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(description="Call Human20 MCP")
    parser.add_argument("method", help="JSON-RPC method or tools/call")
    parser.add_argument("--tool")
    parser.add_argument("--args", default="{}")
    parser.add_argument("--write", action="store_true", help="Allow explicitly owner-authorized board writes only; backend gates still apply")
    args = parser.parse_args()

    client = Human20McpClient(allow_board_writes=args.write)
    if args.method == "tools/call":
        if not args.tool:
            parser.error("--tool is required for tools/call")
        result = client.call_tool(args.tool, json.loads(args.args))
    else:
        result = client.call(args.method, json.loads(args.args))

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
