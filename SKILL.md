---
name: human20-helper
description: Human20 operator helper. Uses the Human20 API/MCP surface to inspect workshop state, Pulse, chat JSON, transcripts, progress, and safe push previews. Read-only by default.
metadata:
  clawdbot:
    triggers:
      - /human20
      - human20
---

# Human20 Helper

Use this skill when an agent needs to understand or operate against Human20 through the official API/MCP surface.

Current scope:
- inspect workshop state and content;
- find and recommend Human20 skills for a user's task;
- read lesson detail/transcripts/homework/favorites/search results;
- compare local OpenClaw state against lesson progression rules;
- guide the user through a test-safe trainer/orchestrator flow for lesson progression.

The skill is intentionally public and contains no secrets. Configure access through local environment variables or a local `.env` file that is not committed.

Required local configuration:

```env
HUMAN20_BEARER_TOKEN=
HUMAN20_MCP_URL=https://human20.app/mcp
```

`HUMAN20_BEARER_TOKEN` may contain either the raw Human20 profile token or `Bearer <token>`.
The helper strips an accidental `Bearer ` prefix before building the Authorization header.

## Guardrails

- Use only documented Human20 API/MCP tools.
- Treat the default workflow as read-only.
- Never call Telegram directly from the skill.
- Never store bearer tokens, Telegram tokens, Supabase keys, exports, or private user data in this repository.
- For outbound user messages, always call `preview_user_message` first and only then `send_user_message` when the operator explicitly confirms.
- If a tool is missing, report it as an API capability gap instead of inventing data.

## Useful Commands

Run from this repository root:

```powershell
python scripts/entrypoint.py status
python scripts/entrypoint.py where-am-i --user-id tg:123
python scripts/entrypoint.py what-new
python scripts/entrypoint.py skill-search "telegram digest"
python scripts/entrypoint.py skill-recommend "какой скил подойдёт для Telegram канала" --human
python scripts/entrypoint.py chat-search "openclaw"
python scripts/entrypoint.py lesson-context lesson-1 --user-id tg:123
python scripts/entrypoint.py "где я сейчас"
python scripts/entrypoint.py "какой скил мне подойдёт для Telegram канала"
python scripts/entrypoint.py "урок 4"
python scripts/entrypoint.py "тестовый режим"
```

## What The Skill Can Inspect

- current workshop/content state;
- Human20 skill catalog search and task-based skill recommendations;
- onboarding state and next recommended move;
- Pulse summaries;
- workshop chat JSON;
- lesson and meeting details;
- transcripts and attachments;
- homework progress;
- local lesson-evidence checks against runtime rules;
- test-safe lesson continuation / trainer flow;
- backend-owned push preview/send tools, when enabled by the API.
