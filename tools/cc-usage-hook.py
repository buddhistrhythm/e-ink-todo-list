#!/usr/bin/env python3
"""
Claude Code Usage Hook - Real-time AI usage tracking for e-ink dashboard.

This script is designed to run as a Claude Code hook on Stop/SessionEnd events.
It reads the transcript JSONL file (passed via stdin), extracts token usage,
and pushes aggregated stats to the einktodo API.

Hook events:
    Stop       - fires each time Claude finishes a response (incremental push)
    SessionEnd - fires when session terminates (final summary push)

Install:
    Add to ~/.claude/settings.json or .claude/settings.json:
    {
      "hooks": {
        "Stop": [{ "hooks": [{ "type": "command", "command": "python3 /path/to/cc-usage-hook.py" }] }],
        "SessionEnd": [{ "hooks": [{ "type": "command", "command": "python3 /path/to/cc-usage-hook.py" }] }]
      }
    }

Environment:
    EINKTODO_API_URL  - API endpoint (default: https://www.einktodo.com/api/display/v2)
    EINKTODO_API_KEY  - API key for authentication
    USAGE_PUSH_MODE   - "realtime" (push on every Stop) or "session" (push on SessionEnd only, default)
    USAGE_LOCAL_LOG   - Path to local usage log file (default: ~/.claude/usage-stats.json)
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

USAGE_LOG = Path(os.environ.get(
    "USAGE_LOCAL_LOG",
    str(Path.home() / ".claude" / "usage-stats.json")
))


def parse_transcript(transcript_path):
    """Parse a Claude Code transcript JSONL file and extract usage stats."""
    stats = {
        "session_id": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 0,
        "api_calls": 0,
        "models": {},
        "tools_used": {},
        "start_time": "",
        "end_time": "",
    }

    if not transcript_path or not Path(transcript_path).exists():
        return stats

    with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Get session ID
            if not stats["session_id"]:
                stats["session_id"] = entry.get("sessionId", "")

            # Track timestamps
            ts = entry.get("timestamp", "")
            if ts:
                if not stats["start_time"]:
                    stats["start_time"] = ts
                stats["end_time"] = ts

            msg = entry.get("message", {})
            if not isinstance(msg, dict):
                continue

            # Extract usage from assistant messages
            usage = msg.get("usage", {})
            if usage:
                input_t = usage.get("input_tokens", 0)
                output_t = usage.get("output_tokens", 0)
                cache_read = usage.get("cache_read_input_tokens", 0)
                cache_creation = usage.get("cache_creation_input_tokens", 0)

                stats["input_tokens"] += input_t
                stats["output_tokens"] += output_t
                stats["cache_read_tokens"] += cache_read
                stats["cache_creation_tokens"] += cache_creation
                stats["total_tokens"] += input_t + output_t
                stats["api_calls"] += 1

                model = msg.get("model", "unknown")
                if model not in stats["models"]:
                    stats["models"][model] = 0
                stats["models"][model] += 1

            # Track tool usage
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool = block.get("name", "unknown")
                        if tool not in stats["tools_used"]:
                            stats["tools_used"][tool] = 0
                        stats["tools_used"][tool] += 1

    return stats


def load_cumulative_stats():
    """Load cumulative usage stats from local log."""
    if USAGE_LOG.exists():
        try:
            with open(USAGE_LOG, "r") as f:
                return json.loads(f.read())
        except (json.JSONDecodeError, IOError):
            pass

    return {
        "tool": "claude_code",
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_cache_creation_tokens": 0,
        "total_tokens": 0,
        "total_api_calls": 0,
        "total_sessions": 0,
        "models": {},
        "tools_used": {},
        "daily": {},
        "sessions": [],
        "last_updated": "",
    }


def save_cumulative_stats(cumulative):
    """Save cumulative stats to local log."""
    USAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    cumulative["last_updated"] = datetime.now().isoformat()
    with open(USAGE_LOG, "w") as f:
        json.dump(cumulative, f, indent=2, default=str)


def merge_stats(cumulative, session_stats):
    """Merge session stats into cumulative stats."""
    cumulative["total_input_tokens"] += session_stats["input_tokens"]
    cumulative["total_output_tokens"] += session_stats["output_tokens"]
    cumulative["total_cache_read_tokens"] += session_stats["cache_read_tokens"]
    cumulative["total_cache_creation_tokens"] += session_stats["cache_creation_tokens"]
    cumulative["total_tokens"] += session_stats["total_tokens"]
    cumulative["total_api_calls"] += session_stats["api_calls"]

    # Merge models
    for model, count in session_stats["models"].items():
        if model not in cumulative["models"]:
            cumulative["models"][model] = 0
        cumulative["models"][model] += count

    # Merge tools
    for tool, count in session_stats["tools_used"].items():
        if tool not in cumulative["tools_used"]:
            cumulative["tools_used"][tool] = 0
        cumulative["tools_used"][tool] += count

    # Daily tracking
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in cumulative["daily"]:
        cumulative["daily"][today] = {"input": 0, "output": 0, "total": 0, "api_calls": 0}
    cumulative["daily"][today]["input"] += session_stats["input_tokens"]
    cumulative["daily"][today]["output"] += session_stats["output_tokens"]
    cumulative["daily"][today]["total"] += session_stats["total_tokens"]
    cumulative["daily"][today]["api_calls"] += session_stats["api_calls"]

    # Session log (keep last 50)
    cumulative["sessions"].append({
        "session_id": session_stats["session_id"],
        "tokens": session_stats["total_tokens"],
        "api_calls": session_stats["api_calls"],
        "start": session_stats["start_time"],
        "end": session_stats["end_time"],
    })
    cumulative["sessions"] = cumulative["sessions"][-50:]
    cumulative["total_sessions"] = cumulative.get("total_sessions", 0) + 1

    return cumulative


def push_to_api(cumulative):
    """Push cumulative stats to einktodo API."""
    api_url = os.environ.get("EINKTODO_API_URL", "https://www.einktodo.com/api/display/v2")
    api_key = os.environ.get("EINKTODO_API_KEY", "")

    if not api_key or not HAS_REQUESTS:
        return False

    url = api_url.rstrip("/") + "/ai-usage"
    payload = {
        "type": "ai_usage",
        "timestamp": datetime.now().isoformat(),
        "source": "claude_code_hook",
        "tools": [cumulative],
        "summary": {
            "total_tokens": cumulative["total_tokens"],
            "total_sessions": cumulative["total_sessions"],
            "total_api_calls": cumulative["total_api_calls"],
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
    # Read hook input from stdin
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        hook_input = {}

    event = hook_input.get("hook_event_name", "")
    transcript_path = hook_input.get("transcript_path", "")
    session_id = hook_input.get("session_id", "")

    push_mode = os.environ.get("USAGE_PUSH_MODE", "session")

    # Parse transcript for usage data
    session_stats = parse_transcript(transcript_path)
    if not session_stats["session_id"] and session_id:
        session_stats["session_id"] = session_id

    if session_stats["total_tokens"] == 0:
        # No usage data found, output empty JSON and exit
        print(json.dumps({}))
        return

    # Load and merge cumulative stats
    cumulative = load_cumulative_stats()

    # For Stop events, we track incrementally
    # For SessionEnd, we do the final push
    if event == "Stop":
        # On each Stop, we re-parse the full transcript to get current totals
        # This avoids double-counting since we parse from scratch each time
        cumulative = load_cumulative_stats()
        # Store current session's running total (will be finalized on SessionEnd)
        cumulative["_current_session"] = {
            "session_id": session_stats["session_id"],
            "tokens": session_stats["total_tokens"],
        }
        save_cumulative_stats(cumulative)

        if push_mode == "realtime":
            push_to_api(cumulative)

    elif event == "SessionEnd":
        # Finalize: merge this session's complete stats
        cumulative.pop("_current_session", None)
        cumulative = merge_stats(cumulative, session_stats)
        save_cumulative_stats(cumulative)
        push_to_api(cumulative)

    else:
        # Generic: merge and save
        cumulative = merge_stats(cumulative, session_stats)
        save_cumulative_stats(cumulative)
        push_to_api(cumulative)

    # Output empty JSON (hook protocol: exit 0 + valid JSON = proceed)
    print(json.dumps({}))


if __name__ == "__main__":
    main()
