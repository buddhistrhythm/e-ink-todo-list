#!/usr/bin/env python3
"""
Codex Plugin: track-usage.py
Invoked by the eink-usage-tracker plugin on Stop events.
Thin wrapper that calls the main codex-usage-hook.py logic.
"""

import importlib.util
import os
import sys
from pathlib import Path

# Resolve the main hook script relative to the plugin directory
plugin_dir = Path(__file__).parent
tools_dir = plugin_dir.parent  # tools/ directory
hook_script = tools_dir / "codex-usage-hook.py"

if not hook_script.exists():
    # Fallback: look for it alongside this script
    hook_script = plugin_dir / "codex-usage-hook.py"

if hook_script.exists():
    spec = importlib.util.spec_from_file_location("codex_usage_hook", str(hook_script))
    if spec and spec.loader:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.main()
else:
    # Inline minimal implementation
    import json
    from datetime import datetime

    try:
        hook_input = json.loads(sys.stdin.read())
    except Exception:
        hook_input = {}

    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    sessions_dir = codex_home / "sessions"
    usage_log = codex_home / "eink-usage-stats.json"

    usage = {
        "tool": "codex",
        "total_tokens": 0,
        "total_sessions": 0,
        "last_updated": datetime.now().isoformat(),
        "note": "Install codex-usage-hook.py in tools/ for full tracking",
    }

    if sessions_dir.exists():
        jsonl_files = list(sessions_dir.rglob("*.jsonl"))
        usage["total_sessions"] = len(jsonl_files)

        for f in jsonl_files:
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        try:
                            event = json.loads(line.strip())
                            payload = event.get("payload", event)
                            if payload.get("type") == "token_count":
                                usage["total_tokens"] = max(
                                    usage["total_tokens"],
                                    payload.get("input_tokens", payload.get("input_token_count", 0))
                                    + payload.get("output_tokens", payload.get("output_token_count", 0))
                                    + payload.get("reasoning_tokens", payload.get("reasoning_token_count", 0))
                                )
                        except json.JSONDecodeError:
                            pass
            except OSError:
                pass

    usage_log.parent.mkdir(parents=True, exist_ok=True)
    with open(usage_log, "w") as f:
        json.dump(usage, f, indent=2)

    print(json.dumps({}))
