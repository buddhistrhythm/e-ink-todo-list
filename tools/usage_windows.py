#!/usr/bin/env python3
"""
usage_windows.py - Time-window based usage tracking for AI coding tools.

Maintains rolling usage windows:
    - 5-hour window (Claude Code billing cycle)
    - Weekly window (Claude Code weekly cap)
    - Monthly window (Cursor quota)
    - Per-model windows (Opus, Sonnet, Haiku separately)

Each window tracks:
    - Tokens consumed within the window
    - Window start/end timestamps
    - When the window resets (next reset time)
    - Usage percentage if limit is known
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# ── Default limits (best-known estimates, user can override) ────────

DEFAULT_LIMITS = {
    "claude_code": {
        "5h": {
            # Per-model 5-hour limits (tokens, approximate)
            "claude-opus-4-6": 500_000,
            "claude-sonnet-4-6": 2_000_000,
            "claude-sonnet-4-5-20250514": 2_000_000,
            "claude-haiku-4-5-20251001": 5_000_000,
            "default": 1_000_000,
        },
        "weekly": {
            "default": 20_000_000,
        },
    },
    "codex": {
        "daily": {
            "default": 10_000_000,
        },
    },
    "cursor": {
        "monthly": {
            "fast_requests": 500,  # Pro plan default
        },
    },
}


class UsageWindow:
    """Tracks usage within a rolling time window."""

    def __init__(self, name: str, duration_hours: float, limit: int = 0):
        self.name = name
        self.duration = timedelta(hours=duration_hours)
        self.limit = limit
        self.entries: list[dict] = []  # [{timestamp, tokens, model}]

    def add(self, tokens: int, model: str = "", timestamp: Optional[datetime] = None):
        ts = timestamp or datetime.now(timezone.utc)
        self.entries.append({
            "timestamp": ts.isoformat(),
            "tokens": tokens,
            "model": model,
        })
        self._prune(ts)

    def _prune(self, now: Optional[datetime] = None):
        """Remove entries outside the window."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - self.duration
        self.entries = [
            e for e in self.entries
            if datetime.fromisoformat(e["timestamp"]) >= cutoff
        ]

    @property
    def total_tokens(self) -> int:
        self._prune()
        return sum(e["tokens"] for e in self.entries)

    @property
    def tokens_by_model(self) -> dict[str, int]:
        self._prune()
        result: dict[str, int] = {}
        for e in self.entries:
            model = e.get("model", "unknown")
            result[model] = result.get(model, 0) + e["tokens"]
        return result

    @property
    def window_start(self) -> Optional[datetime]:
        """Earliest entry in current window."""
        self._prune()
        if not self.entries:
            return None
        timestamps = [datetime.fromisoformat(e["timestamp"]) for e in self.entries]
        return min(timestamps)

    @property
    def window_end(self) -> datetime:
        """When the current window expires (oldest entry falls off)."""
        if not self.entries:
            return datetime.now(timezone.utc)
        oldest = min(datetime.fromisoformat(e["timestamp"]) for e in self.entries)
        return oldest + self.duration

    @property
    def next_reset(self) -> datetime:
        """When the next token budget frees up (oldest entry expires)."""
        return self.window_end

    @property
    def usage_pct(self) -> float:
        if self.limit <= 0:
            return 0.0
        return min(100.0, (self.total_tokens / self.limit) * 100)

    @property
    def remaining(self) -> int:
        if self.limit <= 0:
            return -1  # unknown limit
        return max(0, self.limit - self.total_tokens)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "duration_hours": self.duration.total_seconds() / 3600,
            "limit": self.limit,
            "total_tokens": self.total_tokens,
            "remaining": self.remaining,
            "usage_pct": round(self.usage_pct, 1),
            "tokens_by_model": self.tokens_by_model,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat(),
            "next_reset": self.next_reset.isoformat(),
            "entry_count": len(self.entries),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UsageWindow":
        w = cls(
            name=data["name"],
            duration_hours=data["duration_hours"],
            limit=data.get("limit", 0),
        )
        w.entries = data.get("entries", [])
        return w


class UsageTracker:
    """
    Multi-window usage tracker for a single tool.
    Maintains 5-hour, weekly, and optional monthly windows.
    Also maintains per-model sub-windows.
    """

    def __init__(self, tool: str, limits: Optional[dict] = None):
        self.tool = tool
        tool_limits = limits or DEFAULT_LIMITS.get(tool, {})

        # Main windows
        self.window_5h = UsageWindow(
            "5h", 5.0,
            limit=tool_limits.get("5h", {}).get("default", 0)
        )
        self.window_weekly = UsageWindow(
            "weekly", 7 * 24,
            limit=tool_limits.get("weekly", {}).get("default", 0)
        )
        self.window_monthly = UsageWindow(
            "monthly", 30 * 24,
            limit=tool_limits.get("monthly", {}).get("default", 0)
        )

        # Per-model 5h windows
        self.model_windows: dict[str, UsageWindow] = {}
        self._model_limits = tool_limits.get("5h", {})

    def record(self, tokens: int, model: str = "", timestamp: Optional[datetime] = None):
        """Record token usage across all windows."""
        ts = timestamp or datetime.now(timezone.utc)

        self.window_5h.add(tokens, model, ts)
        self.window_weekly.add(tokens, model, ts)
        self.window_monthly.add(tokens, model, ts)

        # Per-model window
        if model:
            if model not in self.model_windows:
                model_limit = self._model_limits.get(
                    model, self._model_limits.get("default", 0)
                )
                self.model_windows[model] = UsageWindow(
                    f"5h_{model}", 5.0, limit=model_limit
                )
            self.model_windows[model].add(tokens, model, ts)

    def summary(self) -> dict:
        """Generate a full usage summary with all windows."""
        result = {
            "tool": self.tool,
            "windows": {
                "5h": self.window_5h.to_dict(),
                "weekly": self.window_weekly.to_dict(),
                "monthly": self.window_monthly.to_dict(),
            },
            "models": {},
        }

        for model_name, window in self.model_windows.items():
            result["models"][model_name] = window.to_dict()

        return result

    def format_status(self) -> str:
        """Format a human-readable status string."""
        lines = [f"=== {self.tool} Usage ==="]

        for label, window in [
            ("5-Hour", self.window_5h),
            ("Weekly", self.window_weekly),
        ]:
            tokens = window.total_tokens
            if window.limit > 0:
                pct = window.usage_pct
                remaining = window.remaining
                reset = window.next_reset.strftime("%H:%M UTC")
                lines.append(
                    f"  {label}: {_fmt_tokens(tokens)}/{_fmt_tokens(window.limit)} "
                    f"({pct:.0f}%) | {_fmt_tokens(remaining)} left | resets ~{reset}"
                )
            else:
                lines.append(f"  {label}: {_fmt_tokens(tokens)}")

        # Per-model breakdown
        if self.model_windows:
            lines.append("  Models (5h):")
            for model_name, window in sorted(self.model_windows.items()):
                short_name = model_name.split("-")[1] if "-" in model_name else model_name
                tokens = window.total_tokens
                if window.limit > 0:
                    pct = window.usage_pct
                    lines.append(
                        f"    {short_name}: {_fmt_tokens(tokens)}/{_fmt_tokens(window.limit)} ({pct:.0f}%)"
                    )
                else:
                    lines.append(f"    {short_name}: {_fmt_tokens(tokens)}")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize for storage."""
        return {
            "tool": self.tool,
            "window_5h": {**self.window_5h.to_dict(), "entries": self.window_5h.entries},
            "window_weekly": {**self.window_weekly.to_dict(), "entries": self.window_weekly.entries},
            "window_monthly": {**self.window_monthly.to_dict(), "entries": self.window_monthly.entries},
            "model_windows": {
                name: {**w.to_dict(), "entries": w.entries}
                for name, w in self.model_windows.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "UsageTracker":
        tracker = cls(data.get("tool", "unknown"))

        if "window_5h" in data:
            tracker.window_5h = UsageWindow.from_dict(data["window_5h"])
        if "window_weekly" in data:
            tracker.window_weekly = UsageWindow.from_dict(data["window_weekly"])
        if "window_monthly" in data:
            tracker.window_monthly = UsageWindow.from_dict(data["window_monthly"])
        for name, wdata in data.get("model_windows", {}).items():
            tracker.model_windows[name] = UsageWindow.from_dict(wdata)

        return tracker


# ── Cursor quota tracking ───────────────────────────────────────────

class CursorQuota:
    """Track Cursor monthly quota usage and expiration."""

    def __init__(self):
        self.plan = "pro"  # pro, business, enterprise
        self.monthly_fast_requests = 500
        self.used_fast_requests = 0
        self.billing_cycle_start = ""  # ISO date
        self.billing_cycle_end = ""    # ISO date
        self.last_updated = ""

    @property
    def remaining_requests(self) -> int:
        return max(0, self.monthly_fast_requests - self.used_fast_requests)

    @property
    def usage_pct(self) -> float:
        if self.monthly_fast_requests <= 0:
            return 0.0
        return (self.used_fast_requests / self.monthly_fast_requests) * 100

    @property
    def days_remaining(self) -> int:
        if not self.billing_cycle_end:
            return -1
        try:
            end = datetime.fromisoformat(self.billing_cycle_end)
            now = datetime.now(timezone.utc)
            return max(0, (end - now).days)
        except ValueError:
            return -1

    def format_status(self) -> str:
        lines = [
            "=== Cursor Quota ===",
            f"  Plan: {self.plan}",
            f"  Fast requests: {self.used_fast_requests}/{self.monthly_fast_requests} "
            f"({self.usage_pct:.0f}%) | {self.remaining_requests} left",
        ]
        if self.billing_cycle_end:
            lines.append(
                f"  Billing cycle ends: {self.billing_cycle_end} ({self.days_remaining}d left)"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "plan": self.plan,
            "monthly_fast_requests": self.monthly_fast_requests,
            "used_fast_requests": self.used_fast_requests,
            "remaining_requests": self.remaining_requests,
            "usage_pct": round(self.usage_pct, 1),
            "billing_cycle_start": self.billing_cycle_start,
            "billing_cycle_end": self.billing_cycle_end,
            "days_remaining": self.days_remaining,
            "last_updated": self.last_updated,
        }


# ── Helpers ─────────────────────────────────────────────────────────

def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def load_tracker(tool: str, storage_path: Optional[Path] = None) -> UsageTracker:
    """Load a tracker from disk, or create a new one."""
    if storage_path is None:
        storage_path = Path.home() / ".claude" / f"usage-windows-{tool}.json"

    if storage_path.exists():
        try:
            with open(storage_path, "r") as f:
                return UsageTracker.from_dict(json.loads(f.read()))
        except (json.JSONDecodeError, IOError):
            pass

    return UsageTracker(tool)


def save_tracker(tracker: UsageTracker, storage_path: Optional[Path] = None):
    """Save a tracker to disk."""
    if storage_path is None:
        storage_path = Path.home() / ".claude" / f"usage-windows-{tracker.tool}.json"

    storage_path.parent.mkdir(parents=True, exist_ok=True)
    with open(storage_path, "w") as f:
        json.dump(tracker.to_dict(), f, indent=2, default=str)
