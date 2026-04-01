#!/usr/bin/env python3
"""
AI Usage Collector - Collects usage data from Cursor, Claude Code, and OpenAI Codex,
then pushes aggregated stats to the einktodo API for e-ink dashboard display.

Usage:
    python ai-usage-collector.py --api-url <url> --api-key <key>

Data sources:
    - Claude Code: ~/.claude/projects/<project>/<session>.jsonl
      JSONL files with input_tokens, output_tokens, cache tokens, model, timestamps
      (same approach as ccusage - https://github.com/ryoppippi/ccusage)
    - Cursor: Analytics API (Team/Enterprise, set CURSOR_API_KEY)
      or local storage fallback (~/.config/Cursor/ state.vscdb)
    - OpenAI Codex: Analytics API (set OPENAI_API_KEY)
      or local ~/.codex/ session files

Environment variables:
    EINKTODO_API_URL  - einktodo API endpoint (default: einktodo.com)
    EINKTODO_API_KEY  - einktodo API key
    OPENAI_API_KEY    - OpenAI API key for Codex usage API
    CURSOR_API_KEY    - Cursor Analytics API key (Team/Enterprise plans)

The collector pushes a JSON payload to the configured API endpoint,
which the e-ink device periodically fetches as a rendered bitmap.

See also:
    - Claude Code monitoring: https://github.com/anthropics/claude-code-monitoring-guide
    - Cursor Analytics API: https://cursor.com/docs/account/teams/analytics-api
    - OpenTelemetry export: set CLAUDE_CODE_ENABLE_TELEMETRY=1 for OTel metrics
"""

import argparse
import json
import os
import sys
import time
import glob
import re
from datetime import datetime, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("requests library required: pip install requests")
    sys.exit(1)


# ── Claude Code Usage ───────────────────────────────────────────────

def collect_claude_code_usage(days=7):
    """
    Collect Claude Code usage from ~/.claude/projects/ session data.
    Claude Code stores session info in JSONL files under projects/.
    """
    claude_dir = Path.home() / ".claude"
    usage = {
        "tool": "claude_code",
        "sessions": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "models_used": {},
        "daily": {},
    }

    if not claude_dir.exists():
        print("[Claude Code] ~/.claude/ not found, skipping")
        return usage

    cutoff = datetime.now() - timedelta(days=days)

    # Scan projects directory for session JSONL files
    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        # Try sessions directory as fallback
        projects_dir = claude_dir / "sessions"

    if not projects_dir.exists():
        print("[Claude Code] No projects/sessions directory found")
        return usage

    jsonl_files = list(projects_dir.rglob("*.jsonl"))
    print(f"[Claude Code] Found {len(jsonl_files)} session files")

    for jsonl_file in jsonl_files:
        try:
            # Check file modification time
            mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime)
            if mtime < cutoff:
                continue

            with open(jsonl_file, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Extract usage from assistant messages
                    msg = entry.get("message", {})
                    msg_usage = msg.get("usage", {})
                    if not msg_usage:
                        continue

                    input_t = msg_usage.get("input_tokens", 0)
                    output_t = msg_usage.get("output_tokens", 0)
                    cache_read = msg_usage.get("cache_read_input_tokens", 0)
                    cache_creation = msg_usage.get("cache_creation_input_tokens", 0)

                    usage["input_tokens"] += input_t
                    usage["output_tokens"] += output_t
                    usage["cache_read_tokens"] += cache_read
                    usage["cache_creation_tokens"] += cache_creation
                    usage["total_tokens"] += input_t + output_t

                    # Track model usage (confirmed in JSONL structure)
                    model = msg.get("model", "unknown")
                    if model not in usage["models_used"]:
                        usage["models_used"][model] = {"input": 0, "output": 0, "count": 0}
                    usage["models_used"][model]["input"] += input_t
                    usage["models_used"][model]["output"] += output_t
                    usage["models_used"][model]["count"] += 1

                    # Track daily usage
                    ts = entry.get("timestamp", "")
                    if ts:
                        day = ts[:10]  # YYYY-MM-DD
                        if day not in usage["daily"]:
                            usage["daily"][day] = {"input": 0, "output": 0, "total": 0}
                        usage["daily"][day]["input"] += input_t
                        usage["daily"][day]["output"] += output_t
                        usage["daily"][day]["total"] += input_t + output_t

                usage["sessions"] += 1

        except Exception as e:
            print(f"[Claude Code] Error reading {jsonl_file}: {e}")

    print(f"[Claude Code] {usage['sessions']} sessions, {usage['total_tokens']:,} total tokens")
    return usage


# ── Cursor Usage ────────────────────────────────────────────────────

def collect_cursor_usage(days=7):
    """
    Collect Cursor usage from local storage.
    Cursor stores usage data in various locations depending on OS:
    - macOS: ~/Library/Application Support/Cursor/
    - Linux: ~/.config/Cursor/
    - Windows: %APPDATA%/Cursor/
    Also checks ~/.cursor/ for any usage logs.
    """
    usage = {
        "tool": "cursor",
        "sessions": 0,
        "requests": 0,
        "models_used": {},
        "daily": {},
    }

    # Try Cursor Analytics API first (Team/Enterprise plans)
    cursor_api_key = os.environ.get("CURSOR_API_KEY")
    if cursor_api_key:
        try:
            headers = {"Authorization": f"Bearer {cursor_api_key}"}
            resp = requests.get(
                "https://api.cursor.com/v1/analytics/usage",
                headers=headers,
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                usage["requests"] = data.get("totalRequests", 0)
                for entry in data.get("daily", []):
                    day = entry.get("date", "")
                    if day:
                        usage["daily"][day] = {"requests": entry.get("requests", 0)}
                print(f"[Cursor] API: {usage['requests']} requests")
                return usage
            else:
                print(f"[Cursor] API returned {resp.status_code}, falling back to local")
        except Exception as e:
            print(f"[Cursor] API error: {e}, falling back to local")

    # Possible Cursor data locations (local fallback)
    cursor_paths = [
        Path.home() / ".cursor",
        Path.home() / ".config" / "Cursor",
        Path.home() / "Library" / "Application Support" / "Cursor",
    ]

    cursor_dir = None
    for p in cursor_paths:
        if p.exists():
            cursor_dir = p
            break

    if not cursor_dir:
        print("[Cursor] No Cursor directory found, skipping")
        return usage

    cutoff = datetime.now() - timedelta(days=days)

    # Look for usage/telemetry files
    # Cursor uses leveldb/sqlite for state storage
    state_db = cursor_dir / "User" / "globalStorage" / "state.vscdb"
    if state_db.exists():
        print(f"[Cursor] Found state db: {state_db}")
        # State DB is SQLite - try to read
        try:
            import sqlite3
            conn = sqlite3.connect(str(state_db))
            cursor_db = conn.cursor()
            # Look for AI-related keys
            cursor_db.execute(
                "SELECT key, value FROM ItemTable WHERE key LIKE '%usage%' OR key LIKE '%ai%' OR key LIKE '%copilot%'"
            )
            for key, value in cursor_db.fetchall():
                try:
                    data = json.loads(value)
                    if isinstance(data, dict):
                        if "requests" in data:
                            usage["requests"] += data.get("requests", 0)
                        if "totalRequests" in data:
                            usage["requests"] += data.get("totalRequests", 0)
                except (json.JSONDecodeError, TypeError):
                    pass
            conn.close()
        except Exception as e:
            print(f"[Cursor] Error reading state db: {e}")

    # Check for any JSON log files
    log_patterns = [
        cursor_dir / "logs" / "**" / "*.log",
        cursor_dir / "**" / "usage*.json",
    ]

    for pattern in log_patterns:
        for log_file in glob.glob(str(pattern), recursive=True):
            log_path = Path(log_file)
            try:
                mtime = datetime.fromtimestamp(log_path.stat().st_mtime)
                if mtime < cutoff:
                    continue

                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    # Count AI-related log entries
                    ai_entries = len(re.findall(
                        r"(completion|copilot|ai\.request|model\.generate)",
                        content, re.IGNORECASE
                    ))
                    if ai_entries > 0:
                        day = mtime.strftime("%Y-%m-%d")
                        if day not in usage["daily"]:
                            usage["daily"][day] = {"requests": 0}
                        usage["daily"][day]["requests"] += ai_entries
                        usage["requests"] += ai_entries
                        usage["sessions"] += 1
            except Exception as e:
                print(f"[Cursor] Error reading {log_file}: {e}")

    print(f"[Cursor] {usage['requests']} requests found")
    return usage


# ── OpenAI Codex Usage ──────────────────────────────────────────────

def collect_codex_usage(days=7):
    """
    Collect OpenAI Codex/API usage via the OpenAI usage API.
    Requires OPENAI_API_KEY environment variable.
    Also checks ~/.codex/ for local session data.
    """
    usage = {
        "tool": "codex",
        "sessions": 0,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "daily": {},
    }

    # Check local codex directory
    codex_dir = Path.home() / ".codex"
    if codex_dir.exists():
        print(f"[Codex] Found ~/.codex/ directory")
        cutoff = datetime.now() - timedelta(days=days)
        for session_file in codex_dir.rglob("*.json"):
            try:
                mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
                if mtime < cutoff:
                    continue
                with open(session_file, "r", encoding="utf-8", errors="ignore") as f:
                    data = json.loads(f.read())
                    if isinstance(data, dict):
                        u = data.get("usage", {})
                        usage["input_tokens"] += u.get("prompt_tokens", 0)
                        usage["output_tokens"] += u.get("completion_tokens", 0)
                        usage["total_tokens"] += u.get("total_tokens", 0)
                        usage["sessions"] += 1
            except Exception:
                pass

    # Try OpenAI API for usage data
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)

            headers = {"Authorization": f"Bearer {api_key}"}
            # OpenAI usage endpoint
            url = "https://api.openai.com/v1/usage"
            params = {
                "date": start_date.strftime("%Y-%m-%d"),
            }

            resp = requests.get(url, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for entry in data.get("data", []):
                    usage["requests"] += entry.get("n_requests", 0)
                    usage["input_tokens"] += entry.get("n_context_tokens_total", 0)
                    usage["output_tokens"] += entry.get("n_generated_tokens_total", 0)

                    day = datetime.fromtimestamp(
                        entry.get("aggregation_timestamp", 0)
                    ).strftime("%Y-%m-%d")
                    if day not in usage["daily"]:
                        usage["daily"][day] = {"input": 0, "output": 0, "requests": 0}
                    usage["daily"][day]["input"] += entry.get("n_context_tokens_total", 0)
                    usage["daily"][day]["output"] += entry.get("n_generated_tokens_total", 0)
                    usage["daily"][day]["requests"] += entry.get("n_requests", 0)

                usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
                print(f"[Codex] API: {usage['total_tokens']:,} total tokens")
            else:
                print(f"[Codex] API returned {resp.status_code}")
        except Exception as e:
            print(f"[Codex] Error calling OpenAI API: {e}")
    else:
        print("[Codex] No OPENAI_API_KEY set, skipping API usage")

    return usage


# ── Push to e-ink API ───────────────────────────────────────────────

def push_usage(api_url, api_key, usage_data):
    """Push aggregated usage data to the einktodo API."""
    payload = {
        "type": "ai_usage",
        "timestamp": datetime.now().isoformat(),
        "tools": usage_data,
        "summary": {
            "total_tokens": sum(
                t.get("total_tokens", 0) for t in usage_data
            ),
            "total_sessions": sum(
                t.get("sessions", 0) for t in usage_data
            ),
            "total_requests": sum(
                t.get("requests", 0) for t in usage_data
            ),
        },
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    url = api_url.rstrip("/")
    if not url.endswith("/ai-usage"):
        url += "/ai-usage"

    print(f"\nPushing usage data to {url}")
    print(f"Payload summary: {json.dumps(payload['summary'], indent=2)}")

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        print(f"Response: {resp.status_code}")
        if resp.status_code in (200, 201):
            print("Usage data pushed successfully!")
            return True
        else:
            print(f"Error: {resp.text}")
            return False
    except Exception as e:
        print(f"Error pushing usage data: {e}")
        return False


# ── Main ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Collect AI coding tool usage and push to e-ink dashboard"
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("EINKTODO_API_URL", "https://www.einktodo.com/api/display/v2"),
        help="einktodo API URL (default: $EINKTODO_API_URL or einktodo.com)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("EINKTODO_API_KEY", ""),
        help="einktodo API key (default: $EINKTODO_API_KEY)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to look back (default: 7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect data but don't push to API",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Save collected data to a JSON file",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously, pushing every --interval minutes",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Push interval in minutes for daemon mode (default: 60)",
    )

    args = parser.parse_args()

    if not args.api_key and not args.dry_run:
        print("Error: --api-key or EINKTODO_API_KEY required (use --dry-run to skip)")
        sys.exit(1)

    def collect_and_push():
        print(f"\n{'='*60}")
        print(f"AI Usage Collector - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Looking back {args.days} days")
        print(f"{'='*60}\n")

        usage_data = []

        # Collect from all sources
        usage_data.append(collect_claude_code_usage(args.days))
        usage_data.append(collect_cursor_usage(args.days))
        usage_data.append(collect_codex_usage(args.days))

        # Print summary
        print(f"\n{'─'*40}")
        print("Summary:")
        for tool_usage in usage_data:
            tool = tool_usage["tool"]
            tokens = tool_usage.get("total_tokens", 0)
            sessions = tool_usage.get("sessions", 0)
            requests_count = tool_usage.get("requests", 0)
            print(f"  {tool:12s}: {tokens:>12,} tokens | {sessions:>4} sessions | {requests_count:>6} requests")
        print(f"{'─'*40}")

        # Save to file if requested
        if args.output:
            with open(args.output, "w") as f:
                json.dump(usage_data, f, indent=2, default=str)
            print(f"Saved to {args.output}")

        # Push to API
        if not args.dry_run:
            push_usage(args.api_url, args.api_key, usage_data)

    if args.daemon:
        print(f"Running in daemon mode, interval: {args.interval} minutes")
        while True:
            collect_and_push()
            print(f"\nSleeping {args.interval} minutes...")
            time.sleep(args.interval * 60)
    else:
        collect_and_push()


if __name__ == "__main__":
    main()
