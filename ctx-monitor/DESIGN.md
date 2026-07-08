# ctx-monitor — Design Spec

**Date:** 2026-06-10
**Status:** Approved design, pre-implementation
**Owner:** Kiran (personal tooling, lives in `~/.claude/tools/ctx-monitor/`)

## Purpose

When Kiran is away, fleet Claude Code sessions running in tmux burn context unattended.
This tool watches every tmux pane running Claude Code and, when a session's context
window crosses a threshold, drives a checkpoint cycle: **save execution state to a
file → `/compact` → reorient from the file → continue**. A higher threshold sends
`Escape` to interrupt runaway turns so the cycle can actually execute.

## Scope

- **In scope (v1):** Claude Code sessions running inside tmux panes, on this Mac.
  Helpers to (a) launch future ad-hoc sessions inside tmux (`cct` alias) and
  (b) migrate existing non-tmux windows into tmux via `claude --resume` (`adopt.sh`).
- **Out of scope (v1):** non-tmux windows (no injectable channel on macOS — Ghostty
  has no send-text API; reptyr is Linux-only). A hook-based fallback layer for
  non-tmux sessions was designed but deliberately deferred. Always-on/launchd
  operation. Anything token-spending (no LLM in the loop — pure bash/python).

## Components

### 1. Statusline tap (edit to `~/.claude/statusline.sh`)

Claude Code invokes the statusline on every UI render and passes
`context_window.used_percentage` (model-aware — 200k vs 1M handled upstream) plus
`session_id` in the JSON payload. The statusline process inherits `$TMUX_PANE`.

Add ~5 lines after the existing field extraction: when `$TMUX_PANE` is set, write
atomically (tmp file + `mv`) to `/tmp/claude-ctx/<session_id>.json`:

```json
{"session_id": "...", "pct": 37, "pane": "%12", "cwd": "/Users/kiran/...", "ts": 1765400000}
```

Implementation notes:
- Build the JSON with `jq -n` (cwd can contain arbitrary chars; no printf-escaping bugs).
- No-op when not in tmux. Always writes when in tmux (cheap; it is the dashboard's
  data source) — but nothing *acts* on it unless the monitor is running.
- Render cadence caveat: statusline re-renders frequently during active turns and
  sparsely when idle. So `pct` is freshest exactly when it matters (mid-turn), and a
  stale `ts` does NOT mean the session is dead — **pane existence, not file age, is
  the liveness check**.

### 2. Monitor (`ctx-monitor.py`, python3 stdlib only)

Manually started in a terminal; Ctrl+C stops it. Polls every `--tick` (default 5s):

1. Read all `/tmp/claude-ctx/*.json`.
2. Prune entries whose tmux pane no longer exists (`tmux list-panes -a`).
3. Detect busy panes: `tmux capture-pane -p -t <pane>` contains the running-turn
   marker (`esc to interrupt` — verify exact string for installed CC version during
   implementation). Require the marker absent on **two consecutive ticks** before
   declaring idle (guards against capture-during-redraw races).
4. Advance each pane's state machine (below).
5. Render dashboard table (serial `#` / pane / name (`@cc_name` pane option, set
   by /tag) / repo dir / session / context % / state label) + append every
   transition and send to `/tmp/claude-ctx/monitor.log`. The STATE column shows
   human-readable labels (`STATE_LABELS`); internal state names are unchanged
   in logs and the persisted state file.

Startup takes an exclusive non-blocking flock on `.monitor.lock` next to the
script (PID stamped inside); a second instance exits 1 naming the holder PID.
The lock lives alongside the script, NOT in the `/tmp` state dir: macOS reaps
untouched `/tmp` files after ~3 days and would unlink the lock out from under a
long-running daemon, so a second instance could then start unchallenged. Two
concurrent monitors would double-send cycle messages and race the state-file
rename (the persist tmp name is additionally PID-suffixed as belt-and-braces).

### 3. Helpers

- **`cct`** (shell alias/function): launches `claude` inside a solo tmux session named
  after the cwd, so ad-hoc windows are born monitorable. Attaching terminal = the
  Ghostty window the user ran it from.
- **`adopt.sh <cwd> [<cwd>...]`**: builds a tmux session with one pane per given cwd
  (layout per Kiran's 2x2 rules: even-horizontal at 2, tiled at 3-4, new session at 5+),
  each pane running `claude --resume` (interactive picker — user picks the session to
  migrate per pane). For migrating currently-open non-tmux windows: finish/exit in the
  old window, pick it in the picker, close old window.

## State machine (per pane)

```
ARMED --pct>=T1--> SAVE_SENT --idle + checkpoint verified--> COMPACT_SENT --pct<=REARM--> REORIENT_SENT --> ARMED
                      |
                      +-- pct>=T2 and still busy --> send Escape (max 2, 30s grace) --> back to waiting for idle
```

**Thresholds:** `T1 = 35` (save sequence), `T2 = 40` (Escape backstop), `REARM = 20`
(compact considered done). All CLI-overridable (`--t1 --t2 --rearm --tick`).

**Edge-triggered semantics (the resend guarantee):** the `pct >= T1` test is evaluated
ONLY in `ARMED`. One crossing produces exactly one cycle; ticks that still read >= T1
while in any later state do nothing except watch for that state's exit condition. The
monitor itself is the queue — no reliance on Claude Code input-queue behavior for dedup.

### States

- **ARMED** → on `pct >= T1`: send the save message (lands as steering text if a turn
  is running; as a normal prompt if idle). Record cycle start time + checkpoint path.
  → `SAVE_SENT`.
- **SAVE_SENT** →
  - If `pct >= T2` and pane busy: send `Escape` (one keypress). Max 2 attempts, 30s
    apart, loudly logged. (Escape may clear CC's queued-message list — handled by the
    checkpoint verification below.)
  - When pane idle (2 consecutive ticks): **verify the checkpoint** — file exists at
    the expected path with mtime after cycle start. If missing, re-send the save
    message once (handles Escape having eaten the queued message, or the agent
    ignoring it); if missing again after that, mark pane `ERROR`. If present →
    send `C-u` (clear input box) then `/compact` + Enter → `COMPACT_SENT`.
  - No timeout for reaching idle below T2 — a long turn hovering at 36-39% is fine;
    the save message is already queued/steering and will land.
- **COMPACT_SENT** → wait for `pct <= REARM` (compact completion re-renders the
  statusline, so a fresh low pct arrives). Timeout 5 min: re-check idle, retry
  `/compact` once; second failure → `ERROR`. On success → send reorient message →
  `REORIENT_SENT`.
- **REORIENT_SENT** → message sent; immediately re-arm → `ARMED` (next cycle can
  trigger again when the session climbs back past T1).
- **ERROR** → terminal until human looks; shown red on dashboard; never auto-acts again
  on that pane until monitor restart (which resets ERROR panes to ARMED on resume)
  or session change.

### Message templates (configurable at top of script)

Save (T1):
> Hey — we're about to compact your context so you have more room to work with.
> Before that happens, use this opportunity to save your full execution state —
> everything you'd need to pick up exactly where you are — to
> `<cwd>/.cc-checkpoint-<sess8>.md`. Write it now, then wrap up your current step.

Compact: `/compact`

Reorient:
> Your context was just compacted. Read `<cwd>/.cc-checkpoint-<sess8>.md` to reorient
> yourself, then continue from where you left off.

`<sess8>` = first 8 chars of session_id → two panes sharing a cwd cannot collide.

### Send mechanics

Exactly the proven spawn-recipe pattern: `tmux send-keys -t <pane> C-u`, then
`send-keys -t <pane> -l "<text>"`, short real sleep, `send-keys -t <pane> Enter`.
`C-u` first so stale input-box text never corrupts a message. `/compact` is only ever
sent to an idle pane (queued slash commands mid-turn risk being consumed as literal text).

Every send is additionally gated on `#{pane_in_mode}`: a pane in copy-mode (a human
scrolled it) consumes keystrokes itself — Claude Code never sees them — so the monitor
defers the action without mutating any state and retries after the pane leaves the mode
(deferral logged once per streak, not per tick).

## Races & edge cases

| Case | Handling |
|---|---|
| pct still >= T1 on next tick after send | No resend — edge-triggered state machine (see above) |
| Monitor restart mid-cycle | Per-pane state persisted to `/tmp/claude-ctx/monitor-state.json` on every transition; restart resumes exactly where it was (ERROR panes reset to ARMED) |
| Human scrolling a pane (copy-mode) | `#{pane_in_mode}` checked before EVERY send; the action is deferred with no state mutation until the mode exits (logged once per streak) — a pane can never progress toward ERROR while in copy-mode |
| Two monitor instances | Exclusive flock on `<state-dir>/monitor.lock` at startup; the second exits 1 naming the holder PID. Persist tmp files are PID-suffixed so even a rogue writer can't race the rename |
| New session in same pane (`/clear`, relaunch) | session_id in tap file changes → reset pane to `ARMED`, abandon stale cycle. (`/compact` keeps session_id, so normal cycles unaffected) |
| Pane killed mid-cycle | Pruned by pane-existence check; state entry dropped |
| Escape clears CC's queued messages | Checkpoint-verification step re-sends save message once |
| Compact lands above REARM pct | 5-min timeout path: retry once, then ERROR (visible on dashboard) |
| Stale tap file from idle session | Never treated as dead — liveness = pane existence; pct of an idle session is by definition not climbing |
| Two windows attached to one tmux session | Irrelevant — actions key on pane id |
| Dry-run | `--dry-run` logs every would-be send instead of sending |

## Out-of-scope notes for later (v2 candidates)

- **Hook fallback layer** for non-tmux sessions: Stop-hook block-decision for the save,
  PreToolUse deny-all-but-checkpoint-writes as Escape-equivalent, SessionStart
  (source=compact) reorient injection, all gated on the monitor's on-flag. Designed
  2026-06-10, deferred by choice.
- launchd always-on mode.
- Desktop notification (PushNotification/ntfy) when a pane hits ERROR.

## Testing plan

1. Unit-ish: state machine transitions with a fake tap dir + stubbed tmux calls
   (the tmux interface isolated behind a small function for this).
2. `--dry-run` against real running fleet: verify detection, thresholds, no sends.
3. Live e2e: one disposable tmux pane running claude on a junk task,
   `ctx-monitor --t1 5 --t2 8 --rearm 2 --tick 3`; watch a full
   save → compact → reorient → re-arm cycle; confirm checkpoint file content and the
   session genuinely continuing post-compact.
4. Escape path: give the disposable session a deliberately long task, let it cross T2
   busy, confirm Escape lands, checkpoint verification catches a missing file, resend works.

## Success criteria

- A fleet pane crossing T1 completes the full cycle unattended and keeps working.
- No message is ever sent twice for one crossing (except the deliberate
  checkpoint-verification resend).
- Monitor restart mid-cycle does not double-send.
- Kiran can read the dashboard at a glance and trust ERROR to mean "look at me".
