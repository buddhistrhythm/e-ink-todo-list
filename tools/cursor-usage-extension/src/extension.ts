import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import * as https from "https";
import * as http from "http";
import { homedir } from "os";

// ── Types ──────────────────────────────────────────────────────────

interface UsageStats {
  tool: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens?: number;
  api_calls: number;
  models: Record<string, number>;
  daily: Record<string, { input: number; output: number; total: number }>;
  last_updated: string;
}

interface WindowInfo {
  total_tokens: number;
  limit: number;
  remaining: number;
  usage_pct: number;
  window_end: string;
  next_reset: string;
  tokens_by_model: Record<string, number>;
}

interface WindowStats {
  "5h": WindowInfo;
  weekly: WindowInfo;
  models: Record<string, WindowInfo>;
}

interface CursorQuota {
  plan: string;
  monthly_fast_requests: number;
  used_fast_requests: number;
  remaining_requests: number;
  usage_pct: number;
  billing_cycle_end: string;
  days_remaining: number;
}

interface CumulativeStats {
  claude_code: UsageStats;
  codex: UsageStats;
  cursor: UsageStats;
  total_tokens: number;
  last_push: string;
  windows?: {
    claude_code?: WindowStats;
    codex?: WindowStats;
  };
  cursor_quota?: CursorQuota;
}

// ── State ──────────────────────────────────────────────────────────

let statusBarItem: vscode.StatusBarItem;
let pushTimer: NodeJS.Timeout | undefined;
let stats: CumulativeStats;
let claudeWatcher: fs.FSWatcher | undefined;
let codexWatcher: fs.FSWatcher | undefined;

// ── Activation ─────────────────────────────────────────────────────

export function activate(context: vscode.ExtensionContext) {
  stats = loadStats(context);

  // Status bar
  const config = vscode.workspace.getConfiguration("einkUsage");
  if (config.get<boolean>("showStatusBar", true)) {
    statusBarItem = vscode.window.createStatusBarItem(
      vscode.StatusBarAlignment.Right,
      50
    );
    statusBarItem.command = "einkUsage.showStats";
    statusBarItem.tooltip = "AI Usage - Click for details";
    context.subscriptions.push(statusBarItem);
    updateStatusBar();
  }

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand("einkUsage.showStats", () =>
      showStatsPanel(context)
    ),
    vscode.commands.registerCommand("einkUsage.pushNow", () =>
      pushUsage(context)
    ),
    vscode.commands.registerCommand("einkUsage.resetStats", () =>
      resetStats(context)
    )
  );

  // Watch Claude Code JSONL files for real-time tracking
  if (config.get<boolean>("trackClaude", true)) {
    startClaudeWatcher(context);
  }

  // Watch Codex session files
  startCodexWatcher(context);

  // Auto-push timer
  const interval = config.get<number>("pushInterval", 300);
  if (interval > 0) {
    pushTimer = setInterval(() => pushUsage(context), interval * 1000);
    context.subscriptions.push({ dispose: () => clearInterval(pushTimer) });
  }

  // Initial scan
  scanClaudeUsage(context);
  scanCodexUsage(context);
}

export function deactivate() {
  if (claudeWatcher) {
    claudeWatcher.close();
  }
  if (codexWatcher) {
    codexWatcher.close();
  }
  if (pushTimer) {
    clearInterval(pushTimer);
  }
}

// ── Claude Code JSONL Watcher ──────────────────────────────────────

function getClaudeProjectsDir(): string {
  return path.join(homedir(), ".claude", "projects");
}

function startClaudeWatcher(context: vscode.ExtensionContext) {
  const projectsDir = getClaudeProjectsDir();
  if (!fs.existsSync(projectsDir)) {
    return;
  }

  try {
    claudeWatcher = fs.watch(projectsDir, { recursive: true }, (event, filename) => {
      if (filename && filename.endsWith(".jsonl")) {
        // Debounce: wait a bit for writes to complete
        setTimeout(() => scanClaudeUsage(context), 2000);
      }
    });
    context.subscriptions.push({ dispose: () => claudeWatcher?.close() });
  } catch {
    // fs.watch with recursive may not work on all platforms
    // Fall back to periodic scanning
    const scanInterval = setInterval(() => scanClaudeUsage(context), 60000);
    context.subscriptions.push({ dispose: () => clearInterval(scanInterval) });
  }
}

// ── Window Computation ─────────────────────────────────────────────

// Known 5h limits per model (approximate, in tokens)
const MODEL_5H_LIMITS: Record<string, number> = {
  "claude-opus-4-6": 500_000,
  "claude-sonnet-4-6": 2_000_000,
  "claude-sonnet-4-5-20250514": 2_000_000,
  "claude-haiku-4-5-20251001": 5_000_000,
};
const DEFAULT_5H_LIMIT = 1_000_000;
const WEEKLY_LIMIT = 20_000_000;
const FIVE_HOURS_MS = 5 * 60 * 60 * 1000;
const WEEK_MS = 7 * 24 * 60 * 60 * 1000;

interface ApiEntry { timestamp: number; tokens: number; model: string; }

function computeWindows(entries: ApiEntry[]): WindowStats {
  const now = Date.now();
  const fiveHAgo = now - FIVE_HOURS_MS;
  const weekAgo = now - WEEK_MS;

  const entries5h = entries.filter(e => e.timestamp >= fiveHAgo);
  const entriesWeek = entries.filter(e => e.timestamp >= weekAgo);

  // 5h window
  const total5h = entries5h.reduce((s, e) => s + e.tokens, 0);
  const oldest5h = entries5h.length > 0
    ? Math.min(...entries5h.map(e => e.timestamp))
    : now;
  const byModel5h: Record<string, number> = {};
  for (const e of entries5h) {
    byModel5h[e.model] = (byModel5h[e.model] || 0) + e.tokens;
  }

  // Weekly window
  const totalWeek = entriesWeek.reduce((s, e) => s + e.tokens, 0);
  const oldestWeek = entriesWeek.length > 0
    ? Math.min(...entriesWeek.map(e => e.timestamp))
    : now;

  // Per-model 5h windows
  const modelMap: Record<string, ApiEntry[]> = {};
  for (const e of entries5h) {
    if (!modelMap[e.model]) modelMap[e.model] = [];
    modelMap[e.model].push(e);
  }

  const models: Record<string, WindowInfo> = {};
  for (const [model, mEntries] of Object.entries(modelMap)) {
    const mTotal = mEntries.reduce((s, e) => s + e.tokens, 0);
    const mOldest = Math.min(...mEntries.map(e => e.timestamp));
    const mLimit = MODEL_5H_LIMITS[model] || DEFAULT_5H_LIMIT;
    models[model] = {
      total_tokens: mTotal,
      limit: mLimit,
      remaining: Math.max(0, mLimit - mTotal),
      usage_pct: Math.min(100, (mTotal / mLimit) * 100),
      tokens_by_model: { [model]: mTotal },
      window_end: new Date(mOldest + FIVE_HOURS_MS).toISOString(),
      next_reset: new Date(mOldest + FIVE_HOURS_MS).toISOString(),
    };
  }

  return {
    "5h": {
      total_tokens: total5h,
      limit: DEFAULT_5H_LIMIT,
      remaining: Math.max(0, DEFAULT_5H_LIMIT - total5h),
      usage_pct: Math.min(100, (total5h / DEFAULT_5H_LIMIT) * 100),
      tokens_by_model: byModel5h,
      window_end: new Date(oldest5h + FIVE_HOURS_MS).toISOString(),
      next_reset: new Date(oldest5h + FIVE_HOURS_MS).toISOString(),
    },
    weekly: {
      total_tokens: totalWeek,
      limit: WEEKLY_LIMIT,
      remaining: Math.max(0, WEEKLY_LIMIT - totalWeek),
      usage_pct: Math.min(100, (totalWeek / WEEKLY_LIMIT) * 100),
      tokens_by_model: {},
      window_end: new Date(oldestWeek + WEEK_MS).toISOString(),
      next_reset: new Date(oldestWeek + WEEK_MS).toISOString(),
    },
    models,
  };
}

function scanClaudeUsage(context: vscode.ExtensionContext) {
  const projectsDir = getClaudeProjectsDir();
  if (!fs.existsSync(projectsDir)) {
    return;
  }

  const today = new Date().toISOString().slice(0, 10);
  const cutoffMs = Date.now() - 7 * 24 * 60 * 60 * 1000; // 7 days

  const usage: UsageStats = {
    tool: "claude_code",
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_read_tokens: 0,
    api_calls: 0,
    models: {},
    daily: {},
    last_updated: new Date().toISOString(),
  };

  // Collect per-call entries for window computation
  const apiEntries: ApiEntry[] = [];

  try {
    const jsonlFiles = findJsonlFiles(projectsDir, cutoffMs);

    for (const filePath of jsonlFiles) {
      try {
        const content = fs.readFileSync(filePath, "utf-8");
        for (const line of content.split("\n")) {
          if (!line.trim()) continue;
          try {
            const entry = JSON.parse(line);
            const msg = entry.message;
            if (!msg || !msg.usage) continue;

            const u = msg.usage;
            const inputT = u.input_tokens || 0;
            const outputT = u.output_tokens || 0;

            usage.input_tokens += inputT;
            usage.output_tokens += outputT;
            usage.total_tokens += inputT + outputT;
            usage.cache_read_tokens += u.cache_read_input_tokens || 0;
            usage.api_calls++;

            const model = msg.model || "unknown";
            usage.models[model] = (usage.models[model] || 0) + 1;

            const ts = entry.timestamp;
            if (ts) {
              const day = ts.slice(0, 10);
              if (!usage.daily[day]) {
                usage.daily[day] = { input: 0, output: 0, total: 0 };
              }
              usage.daily[day].input += inputT;
              usage.daily[day].output += outputT;
              usage.daily[day].total += inputT + outputT;

              // Collect for window tracking
              apiEntries.push({
                timestamp: new Date(ts).getTime(),
                tokens: inputT + outputT,
                model,
              });
            }
          } catch {
            // skip malformed lines
          }
        }
      } catch {
        // skip unreadable files
      }
    }
  } catch {
    // directory read error
  }

  stats.claude_code = usage;

  // Compute 5h / weekly / per-model windows
  if (!stats.windows) stats.windows = {};
  stats.windows.claude_code = computeWindows(apiEntries);

  stats.total_tokens =
    usage.total_tokens +
    (stats.codex?.total_tokens || 0) +
    (stats.cursor?.total_tokens || 0);
  saveStats(context, stats);
  updateStatusBar();
}

function findJsonlFiles(dir: string, cutoffMs: number): string[] {
  const results: string[] = [];

  function walk(currentDir: string) {
    try {
      const entries = fs.readdirSync(currentDir, { withFileTypes: true });
      for (const entry of entries) {
        const fullPath = path.join(currentDir, entry.name);
        if (entry.isDirectory()) {
          walk(fullPath);
        } else if (entry.name.endsWith(".jsonl")) {
          try {
            const stat = fs.statSync(fullPath);
            if (stat.mtimeMs >= cutoffMs) {
              results.push(fullPath);
            }
          } catch {
            // skip
          }
        }
      }
    } catch {
      // skip inaccessible dirs
    }
  }

  walk(dir);
  return results;
}

// ── Codex JSONL Watcher ────────────────────────────────────────────

function getCodexSessionsDir(): string {
  return process.env.CODEX_HOME
    ? path.join(process.env.CODEX_HOME, "sessions")
    : path.join(homedir(), ".codex", "sessions");
}

function startCodexWatcher(context: vscode.ExtensionContext) {
  const sessionsDir = getCodexSessionsDir();
  if (!fs.existsSync(sessionsDir)) {
    return;
  }

  try {
    codexWatcher = fs.watch(sessionsDir, { recursive: true }, (event, filename) => {
      if (filename && filename.endsWith(".jsonl")) {
        setTimeout(() => scanCodexUsage(context), 2000);
      }
    });
    context.subscriptions.push({ dispose: () => codexWatcher?.close() });
  } catch {
    const scanInterval = setInterval(() => scanCodexUsage(context), 60000);
    context.subscriptions.push({ dispose: () => clearInterval(scanInterval) });
  }
}

function scanCodexUsage(context: vscode.ExtensionContext) {
  const sessionsDir = getCodexSessionsDir();
  if (!fs.existsSync(sessionsDir)) {
    return;
  }

  const cutoffMs = Date.now() - 7 * 24 * 60 * 60 * 1000;

  const usage: UsageStats = {
    tool: "codex",
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_read_tokens: 0,
    reasoning_tokens: 0,
    api_calls: 0,
    models: {},
    daily: {},
    last_updated: new Date().toISOString(),
  };

  const jsonlFiles = findJsonlFiles(sessionsDir, cutoffMs);

  for (const filePath of jsonlFiles) {
    let lastTokenCount: any = null;
    let fileModel = "unknown";
    let fileDay = "";

    try {
      const content = fs.readFileSync(filePath, "utf-8");
      for (const line of content.split("\n")) {
        if (!line.trim()) continue;
        try {
          const event = JSON.parse(line);
          const payload = event.payload || event;
          const eventType = payload.type || event.type || "";

          if (eventType === "token_count") {
            lastTokenCount = payload;
          }

          const model = payload.model || event.model;
          if (model) fileModel = model;

          const ts = event.timestamp || event.ts;
          if (ts && !fileDay) fileDay = ts.slice(0, 10);
        } catch {
          // skip
        }
      }
    } catch {
      continue;
    }

    if (!lastTokenCount) continue;

    // Codex token_count is cumulative per session - take the last one
    const inputT = lastTokenCount.input_tokens || lastTokenCount.input_token_count || 0;
    const outputT = lastTokenCount.output_tokens || lastTokenCount.output_token_count || 0;
    const reasoningT = lastTokenCount.reasoning_tokens || lastTokenCount.reasoning_token_count || 0;
    const sessionTotal = inputT + outputT + reasoningT;

    usage.input_tokens += inputT;
    usage.output_tokens += outputT;
    usage.reasoning_tokens = (usage.reasoning_tokens || 0) + reasoningT;
    usage.total_tokens += sessionTotal;
    usage.api_calls++;

    usage.models[fileModel] = (usage.models[fileModel] || 0) + 1;

    if (!fileDay) {
      try {
        fileDay = new Date(fs.statSync(filePath).mtimeMs).toISOString().slice(0, 10);
      } catch {
        fileDay = new Date().toISOString().slice(0, 10);
      }
    }
    if (!usage.daily[fileDay]) {
      usage.daily[fileDay] = { input: 0, output: 0, total: 0 };
    }
    usage.daily[fileDay].input += inputT;
    usage.daily[fileDay].output += outputT;
    usage.daily[fileDay].total += sessionTotal;
  }

  stats.codex = usage;
  stats.total_tokens =
    (stats.claude_code?.total_tokens || 0) +
    usage.total_tokens +
    (stats.cursor?.total_tokens || 0);
  saveStats(context, stats);
  updateStatusBar();
}

// ── Status Bar ─────────────────────────────────────────────────────

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return `${n}`;
}

function updateStatusBar() {
  if (!statusBarItem) return;

  // Show 5h window usage if available, otherwise total
  const w5h = stats.windows?.claude_code?.["5h"];
  if (w5h && w5h.limit > 0) {
    const pct = w5h.usage_pct;
    const resetTime = w5h.next_reset
      ? new Date(w5h.next_reset).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : "?";
    statusBarItem.text = `$(pulse) 5h: ${fmtTokens(w5h.total_tokens)}/${fmtTokens(w5h.limit)} (${pct.toFixed(0)}%)`;
    statusBarItem.tooltip = `5h window: ${pct.toFixed(0)}% used | Resets ~${resetTime}\nWeekly: ${fmtTokens(stats.windows?.claude_code?.weekly?.total_tokens || 0)}\nTotal (all tools): ${fmtTokens(stats.total_tokens)}`;
  } else {
    statusBarItem.text = `$(pulse) AI: ${fmtTokens(stats.total_tokens || 0)} tokens`;
    statusBarItem.tooltip = "AI Usage - Click for details";
  }

  statusBarItem.show();
}

// ── Stats Panel ────────────────────────────────────────────────────

function fmtWindowTime(isoStr: string): string {
  if (!isoStr) return "?";
  try {
    const d = new Date(isoStr);
    return d.toLocaleString([], {
      month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return isoStr; }
}

function renderWindowSection(label: string, w: WindowInfo | undefined): string[] {
  if (!w) return [];
  const lines: string[] = [];
  const used = fmtTokens(w.total_tokens);
  const limit = w.limit > 0 ? fmtTokens(w.limit) : "?";
  const pct = w.limit > 0 ? ` (${w.usage_pct.toFixed(0)}%)` : "";
  const remaining = w.remaining >= 0 ? fmtTokens(w.remaining) : "?";
  const reset = fmtWindowTime(w.next_reset);

  lines.push(`| ${label} | ${used} / ${limit}${pct} | ${remaining} left | resets ~${reset} |`);
  return lines;
}

function showStatsPanel(context: vscode.ExtensionContext) {
  const cc = stats.claude_code;
  const cx = stats.codex;
  const ccWin = stats.windows?.claude_code;
  const cxWin = stats.windows?.codex;
  const quota = stats.cursor_quota;
  const today = new Date().toISOString().slice(0, 10);

  const lines = [
    `# AI Usage Dashboard`,
    ``,
    `**Total tokens (all tools):** ${(stats.total_tokens || 0).toLocaleString()}`,
    ``,
  ];

  // ── Usage Windows ──
  if (ccWin || cxWin || quota) {
    lines.push(
      `## Usage Windows`,
      `| Window | Used / Limit | Remaining | Reset |`,
      `|--------|-------------|-----------|-------|`
    );

    if (ccWin) {
      lines.push(...renderWindowSection("Claude 5h", ccWin["5h"]));
      lines.push(...renderWindowSection("Claude Weekly", ccWin.weekly));
    }
    if (cxWin) {
      lines.push(...renderWindowSection("Codex 5h", cxWin["5h"]));
    }
    if (quota) {
      const qUsed = `${quota.used_fast_requests} / ${quota.monthly_fast_requests} reqs (${quota.usage_pct.toFixed(0)}%)`;
      const qRemain = `${quota.remaining_requests} reqs`;
      const qEnd = quota.billing_cycle_end
        ? `${quota.billing_cycle_end} (${quota.days_remaining}d)`
        : "?";
      lines.push(`| Cursor Monthly | ${qUsed} | ${qRemain} | ${qEnd} |`);
    }
    lines.push(``);

    // Per-model windows
    const modelWindows = ccWin?.models || {};
    if (Object.keys(modelWindows).length > 0) {
      lines.push(
        `### Per-Model (5h window)`,
        `| Model | Used / Limit | % | Reset |`,
        `|-------|-------------|---|-------|`
      );
      for (const [model, mw] of Object.entries(modelWindows)) {
        const short = model.replace(/^claude-/, "").replace(/-\d{8}$/, "");
        const used = fmtTokens(mw.total_tokens);
        const limit = mw.limit > 0 ? fmtTokens(mw.limit) : "?";
        const pct = mw.limit > 0 ? `${mw.usage_pct.toFixed(0)}%` : "-";
        const reset = fmtWindowTime(mw.next_reset);
        lines.push(`| ${short} | ${used} / ${limit} | ${pct} | ~${reset} |`);
      }
      lines.push(``);
    }
  }

  // ── Claude Code 7-day totals ──
  if (cc && cc.total_tokens > 0) {
    const todayCC = cc.daily?.[today];
    lines.push(
      `## Claude Code (7-day totals)`,
      `| Metric | Value |`,
      `|--------|-------|`,
      `| Input tokens | ${(cc.input_tokens || 0).toLocaleString()} |`,
      `| Output tokens | ${(cc.output_tokens || 0).toLocaleString()} |`,
      `| Cache read | ${(cc.cache_read_tokens || 0).toLocaleString()} |`,
      `| Total tokens | ${(cc.total_tokens || 0).toLocaleString()} |`,
      `| API calls | ${(cc.api_calls || 0).toLocaleString()} |`,
      ``
    );
    if (todayCC) {
      lines.push(`**Today:** ${todayCC.total.toLocaleString()} tokens`, ``);
    }
  }

  // ── Codex 7-day totals ──
  if (cx && cx.total_tokens > 0) {
    const todayCX = cx.daily?.[today];
    lines.push(
      `## Codex (7-day totals)`,
      `| Metric | Value |`,
      `|--------|-------|`,
      `| Input tokens | ${(cx.input_tokens || 0).toLocaleString()} |`,
      `| Output tokens | ${(cx.output_tokens || 0).toLocaleString()} |`,
      `| Reasoning | ${(cx.reasoning_tokens || 0).toLocaleString()} |`,
      `| Total tokens | ${(cx.total_tokens || 0).toLocaleString()} |`,
      ``
    );
    if (todayCX) {
      lines.push(`**Today:** ${todayCX.total.toLocaleString()} tokens`, ``);
    }
  }

  // ── Combined daily ──
  const allDays = new Set([
    ...Object.keys(cc?.daily || {}),
    ...Object.keys(cx?.daily || {}),
  ]);
  if (allDays.size > 0) {
    lines.push(
      `## Daily Breakdown`,
      `| Date | Claude Code | Codex | Total |`,
      `|------|-------------|-------|-------|`
    );
    const sortedDays = [...allDays].sort((a, b) => b.localeCompare(a)).slice(0, 7);
    for (const day of sortedDays) {
      const ccDay = cc?.daily?.[day]?.total || 0;
      const cxDay = cx?.daily?.[day]?.total || 0;
      lines.push(
        `| ${day} | ${ccDay.toLocaleString()} | ${cxDay.toLocaleString()} | ${(ccDay + cxDay).toLocaleString()} |`
      );
    }
  }

  const doc = lines.join("\n");

  vscode.workspace.openTextDocument({ content: doc, language: "markdown" }).then(
    (document) => {
      vscode.window.showTextDocument(document, { preview: true });
    }
  );
}

// ── Push to API ────────────────────────────────────────────────────

function pushUsage(context: vscode.ExtensionContext) {
  const config = vscode.workspace.getConfiguration("einkUsage");
  const apiUrl = config.get<string>("apiUrl", "");
  const apiKey = config.get<string>("apiKey", "");

  if (!apiUrl || !apiKey) {
    vscode.window.showWarningMessage(
      "E-Ink Usage: Set einkUsage.apiUrl and einkUsage.apiKey in settings"
    );
    return;
  }

  const url = new URL(apiUrl.replace(/\/$/, "") + "/ai-usage");
  const payload = JSON.stringify({
    type: "ai_usage",
    timestamp: new Date().toISOString(),
    source: "cursor_extension",
    tools: [stats.claude_code, stats.codex, stats.cursor].filter(Boolean),
    summary: {
      total_tokens: stats.total_tokens,
    },
  });

  const options = {
    hostname: url.hostname,
    port: url.port || (url.protocol === "https:" ? 443 : 80),
    path: url.pathname + url.search,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
      "Content-Length": Buffer.byteLength(payload),
    },
  };

  const transport = url.protocol === "https:" ? https : http;
  const req = transport.request(options, (res) => {
    if (res.statusCode && res.statusCode >= 200 && res.statusCode < 300) {
      stats.last_push = new Date().toISOString();
      saveStats(context, stats);
      vscode.window.showInformationMessage("E-Ink Usage: Pushed to dashboard!");
    } else {
      vscode.window.showWarningMessage(
        `E-Ink Usage: Push failed (${res.statusCode})`
      );
    }
  });

  req.on("error", (err) => {
    vscode.window.showErrorMessage(`E-Ink Usage: ${err.message}`);
  });

  req.write(payload);
  req.end();
}

// ── Persistence ────────────────────────────────────────────────────

function loadStats(context: vscode.ExtensionContext): CumulativeStats {
  const saved = context.globalState.get<CumulativeStats>("einkUsageStats");
  if (saved) return saved;

  return {
    claude_code: emptyUsageStats("claude_code"),
    codex: emptyUsageStats("codex"),
    cursor: emptyUsageStats("cursor"),
    total_tokens: 0,
    last_push: "",
  };
}

function saveStats(context: vscode.ExtensionContext, stats: CumulativeStats) {
  context.globalState.update("einkUsageStats", stats);
}

function resetStats(context: vscode.ExtensionContext) {
  stats = {
    claude_code: emptyUsageStats("claude_code"),
    codex: emptyUsageStats("codex"),
    cursor: emptyUsageStats("cursor"),
    total_tokens: 0,
    last_push: "",
  };
  saveStats(context, stats);
  updateStatusBar();
  vscode.window.showInformationMessage("E-Ink Usage: Stats reset");
}

function emptyUsageStats(tool: string): UsageStats {
  return {
    tool,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_read_tokens: 0,
    api_calls: 0,
    models: {},
    daily: {},
    last_updated: "",
  };
}
