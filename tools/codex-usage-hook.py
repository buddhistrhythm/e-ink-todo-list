#!/usr/bin/env python3
"""
Codex CLI Usage Hook - Real-time AI usage tracking for e-ink dashboard.

Codex CLI hooks system supports: PreToolUse, PostToolUse, UserPromptSubmit, Stop.
This hook runs on Stop events, parses the session JSONL from ~/.codex/sessions/,
extracts token_count events, and pushes aggregated usage to the einktodo API.

Codex session JSONL format:
    - Events with payload.type === "token_count" contain cumulative totals
    - Fields: input_tokens, cached_input_tokens, output_tokens, reasoning_tokens
    - CLI subtracts previous totals to get per-turn delta

Install:
    Place hooks.json next to your ~/.codex/config.toml:

    ~/.codex/hooks.json:
    [
      {
        "hook_type": "Stop",
        "command": "python3 /path/to/codex-usage-hook.py"
      }
    ]

Environment:
    EINKTODO_API_URL  - API endpoint
    EINKTODO_API_KEY  - API key
    CODEX_HOME        - Codex data dir (default: ~/.codex)
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

CODEX_HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
USAGE_LOG = CODEX_HOME / "eink-usage-stats.json"


def find_recent_sessions(days=7):
    """Find recent session JSONL files in ~/.codex/sessions/."""
    sessions_dir = CODEX_HOME / "sessions"
    if not sessions_dir.exists():
        return []

    cutoff = datetime.now() - timedelta(days=days)
    results = []

    for f in sessions_dir.rglob("*.jsonl"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime >= cutoff:
                results.append(f)
        except OSError:
            continue

    return results


def parse_session_file(filepath):
    """
    Parse a Codex session JSONL file.

    Codex token_count events report cumulative totals per session.
    We take the last token_count event as the session total.
    """
    session_stats = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "tool_calls": 0,
        "turns": 0,
        "model": "",
        "start_time": "",
        "end_time": "",
    }

    last_token_count = None

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Track timestamps
            ts = event.get("timestamp", event.get("ts", ""))
            if ts:
                if not session_stats["start_time"]:
                    session_stats["start_time"] = ts
                session_stats["end_time"] = ts

            payload = event.get("payload", event)
            event_type = payload.get("type", event.get("type", ""))

            # Token count events (cumulative)
            if event_type == "token_count":
                last_token_count = payload

            # Track tool usage
            if event_type in ("tool_use", "tool_call", "function_call"):
                session_stats["tool_calls"] += 1

            # Track turns
            if event_type in ("user_message", "user_prompt"):
                session_stats["turns"] += 1

            # Track model
            model = payload.get("model", event.get("model", ""))
            if model:
                session_stats["model"] = model

    # Use last cumulative token_count as session total
    if last_token_count:
        session_stats["input_tokens"] = last_token_count.get("input_tokens",
            last_token_count.get("input_token_count", 0))
        session_stats["output_tokens"] = last_token_count.get("output_tokens",
            last_token_count.get("output_token_count", 0))
        session_stats["cached_input_tokens"] = last_token_count.get("cached_input_tokens",
            last_token_count.get("cached_token_count", 0))
        session_stats["reasoning_tokens"] = last_token_count.get("reasoning_tokens",
            last_token_count.get("reasoning_token_count", 0))
        session_stats["total_tokens"] = (
            session_stats["input_tokens"]
            + session_stats["output_tokens"]
            + session_stats["reasoning_tokens"]
        )

    return session_stats


def scan_all_sessions(days=7):
    """Scan all recent Codex sessions and aggregate usage."""
    usage = {
        "tool": "codex",
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "total_sessions": 0,
        "tool_calls": 0,
        "models": {},
        "daily": {},
        "last_updated": datetime.now().isoformat(),
    }

    session_files = find_recent_sessions(days)

    for filepath in session_files:
        stats = parse_session_file(filepath)

        if stats["total_tokens"] == 0:
            continue

        usage["input_tokens"] += stats["input_tokens"]
        usage["output_tokens"] += stats["output_tokens"]
        usage["cached_input_tokens"] += stats["cached_input_tokens"]
        usage["reasoning_tokens"] += stats["reasoning_tokens"]
        usage["total_tokens"] += stats["total_tokens"]
        usage["tool_calls"] += stats["tool_calls"]
        usage["total_sessions"] += 1

        # Model tracking
        model = stats["model"] or "unknown"
        if model not in usage["models"]:
            usage["models"][model] = 0
        usage["models"][model] += 1

        # Daily tracking (use start_time date)
        day = ""
        if stats["start_time"]:
            day = stats["start_time"][:10]
        if not day:
            try:
                day = datetime.fromtimestamp(filepath.stat().st_mtime).strftime("%Y-%m-%d")
            except OSError:
                day = datetime.now().strftime("%Y-%m-%d")

        if day not in usage["daily"]:
            usage["daily"][day] = {"input": 0, "output": 0, "reasoning": 0, "total": 0, "sessions": 0}
        usage["daily"][day]["input"] += stats["input_tokens"]
        usage["daily"][day]["output"] += stats["output_tokens"]
        usage["daily"][day]["reasoning"] += stats["reasoning_tokens"]
        usage["daily"][day]["total"] += stats["total_tokens"]
        usage["daily"][day]["sessions"] += 1

    return usage


def save_usage(usage):
    """Save usage stats to local file."""
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(USAGE_LOG, "w") as f:
        json.dump(usage, f, indent=2, default=str)


def push_to_api(usage):
    """Push usage data to einktodo API."""
    api_url = os.environ.get("EINKTODO_API_URL", "https://www.einktodo.com/api/display/v2")
    api_key = os.environ.get("EINKTODO_API_KEY", "")

    if not api_key or not HAS_REQUESTS:
        return False

    url = api_url.rstrip("/") + "/ai-usage"
    payload = {
        "type": "ai_usage",
        "timestamp": datetime.now().isoformat(),
        "source": "codex_hook",
        "tools": [usage],
        "summary": {
            "total_tokens": usage["total_tokens"],
            "total_sessions": usage["total_sessions"],
            "reasoning_tokens": usage["reasoning_tokens"],
        },
    }

    try:
        resp = requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        return resp.status_code in (200, 201)
    except Exception:
        return False


def main():
    # Read hook input from stdin (Codex hooks receive JSON on stdin)
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        hook_input = {}

    # Scan all recent sessions
    usage = scan_all_sessions(days=7)
    save_usage(usage)

    # Push to API if configured
    if os.environ.get("EINKTODO_API_KEY"):
        push_to_api(usage)

    # Hook protocol: output JSON on stdout, exit 0 = proceed
    print(json.dumps({}))


if __name__ == "__main__":
    main()
