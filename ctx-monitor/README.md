# ctx-monitor

Watches every tmux pane running Claude Code on this Mac and, when a session's
context window crosses T1 (35%), drives an unattended checkpoint cycle:

    save execution state to a file  ->  /compact  ->  reorient  ->  continue

At T2 (40%) it sends Escape (max 2, 30s apart) to interrupt a runaway turn so
the cycle can execute. Pure stdlib python3 + tmux; no LLM in the loop, no
dependencies. Spec: `DESIGN.md`.

## Prerequisites

- The statusline tap (a block inside `~/.claude/statusline.sh`) must be
  installed — it publishes `/tmp/claude-ctx/<session_id>.json` on every
  statusline render of a tmux-resident session. No tap files = empty dashboard.
- Only sessions INSIDE tmux are monitorable (v1 scope). Launch ad-hoc ones
  with `cct`, migrate existing windows with `./adopt.sh <cwd> ...`.

### Installing the statusline tap

Paste this into `~/.claude/statusline.sh` (requires `jq`). The tap is a no-op
outside tmux, swallows every failure so it can never break your statusline, and
writes atomically (tmp file + `mv`). The `SESSION_ID`/`PCT`/`DIR`/... field
extractions at the top read Claude Code's status JSON from stdin — skip any you
already extract in your own statusline and keep only the tap block:

```sh
# --- Read Claude Code's status JSON (skip lines your statusline already has) ---
input=$(cat)
SESSION_ID=$(echo "$input" | jq -r '.session_id // empty')
PCT=$(echo "$input" | jq -r '.context_window.used_percentage // 0' | cut -d. -f1)
DIR=$(echo "$input" | jq -r '.workspace.current_dir')
MODEL=$(echo "$input" | jq -r '.model.display_name')
EFFORT=$(echo "$input" | jq -r '.effort.level // empty')
WORKTREE=$(echo "$input" | jq -r '.worktree.name // empty')
FIVE_H=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
FIVE_H_RESET=$(echo "$input" | jq -r '.rate_limits.five_hour.resets_at // empty')
SEVEN_D=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')
SEVEN_D_RESET=$(echo "$input" | jq -r '.rate_limits.seven_day.resets_at // empty')

# --- ctx-monitor tap ---
# Publishes context usage for tmux-resident sessions. No-op when not in tmux.
# Inputs are sanitized: SESSION_ID is constrained to UUID characters so it
# cannot escape CTX_DIR via path traversal; PCT is forced to an integer; the
# ts command substitution has a 0 fallback.
if [ -n "$TMUX_PANE" ] && [ -n "$SESSION_ID" ]; then
    case "$SESSION_ID" in (*[!0-9A-Fa-f-]*) SESSION_ID="" ;; esac
    case "$PCT" in (''|*[!0-9]*) PCT=0 ;; esac
    if [ -n "$SESSION_ID" ]; then
        CTX_DIR="/tmp/claude-ctx"
        mkdir -p "$CTX_DIR" 2>/dev/null
        CTX_TMP=$(mktemp "$CTX_DIR/.tap.XXXXXX" 2>/dev/null) && \
        jq -n \
            --arg session_id "$SESSION_ID" \
            --argjson pct "${PCT:-0}" \
            --arg pane "$TMUX_PANE" \
            --arg cwd "$DIR" \
            --argjson ts "$(date +%s 2>/dev/null || echo 0)" \
            --arg model "${MODEL:-}" \
            --arg effort "${EFFORT:-}" \
            --arg worktree "${WORKTREE:-}" \
            --arg five_h_pct "${FIVE_H:-}" \
            --arg five_h_reset "${FIVE_H_RESET:-}" \
            --arg seven_d_pct "${SEVEN_D:-}" \
            --arg seven_d_reset "${SEVEN_D_RESET:-}" \
            '{session_id: $session_id, pct: $pct, pane: $pane, cwd: $cwd, ts: $ts, model: $model, effort: $effort, worktree: $worktree, five_h_pct: $five_h_pct, five_h_reset: $five_h_reset, seven_d_pct: $seven_d_pct, seven_d_reset: $seven_d_reset}' \
            > "$CTX_TMP" 2>/dev/null && \
        mv "$CTX_TMP" "$CTX_DIR/$SESSION_ID.json" 2>/dev/null || rm -f "$CTX_TMP" 2>/dev/null
    fi
fi
# --- end ctx-monitor tap ---
```

## Run

    cd ~/.claude/tools/ctx-monitor
    python3 ctx-monitor.py                 # real run, defaults
    python3 ctx-monitor.py --dry-run       # log would-be sends, send nothing

Ctrl+C stops it. Flags (defaults shown):
`--t1 35 --t2 40 --rearm 20 --tick 5 --state-dir /tmp/claude-ctx --dry-run`

Only ONE instance can run: startup takes an exclusive flock on
`.monitor.lock` next to the script (PID stamped inside). A second copy exits 1
with an error naming the holder PID. The lock releases automatically on process
death — no stale-lock cleanup needed. The lock lives alongside the script, NOT
in the `/tmp` state dir, because macOS reaps untouched `/tmp` files after ~3
days and would unlink the lock out from under a long-running daemon — letting a
second instance start unchallenged.

After a `--dry-run` session, remove the persisted state before a real run
(dry-run suppresses sends but still records state transitions):

    rm -f /tmp/claude-ctx/monitor-state.json

### Run always-on (launchd)

To keep the monitor running unattended — and relaunch it at every login —
register the LaunchAgent instead of running it in a terminal:

    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/sg.lexi.ctx-monitor.plist
    launchctl enable gui/$(id -u)/sg.lexi.ctx-monitor

(`launchctl load -w` is the deprecated spelling — prefer `bootstrap`.) If it
errors with "already bootstrapped", boot it out first:
`launchctl bootout gui/$(id -u)/sg.lexi.ctx-monitor`. That bootout-then-bootstrap
cycle is also how you make launchd pick up an **edited** plist — it caches the
old one otherwise.

The plist (label `sg.lexi.ctx-monitor`) has `RunAtLoad` + `KeepAlive`, so this
starts the monitor now and relaunches it on every login (and if it ever dies).
Its `ProgramArguments` run `~/.claude/tools/ctx-monitor/ctx-monitor.py`, which
resolves through the symlink into this repo — so it **depends on the
`~/.claude/tools/ctx-monitor` symlink existing** (see the cockpit's
[Install](../README.md#install-fresh-machine) step 4). stderr is captured to
`~/Library/Logs/ctx-monitor.err`.

> **Why not `/tmp/claude-ctx/monitor.err`?** launchd opens `StandardErrorPath`
> *before* exec'ing the job and does **not** create intermediate directories.
> The monitor makes its own state dir, but that runs far too late — and macOS
> reaps untouched `/tmp` files after ~3 days. So a `/tmp` log path meant launchd
> could silently fail to spawn the job at login. `~/Library/Logs` always exists
> and is never reaped.

To stop:

    launchctl bootout gui/$(id -u)/sg.lexi.ctx-monitor

The foreground `python3 ctx-monitor.py` run above is still fine for ad-hoc use.
Only one instance can run regardless (the `.monitor.lock` flock), so the
LaunchAgent and a stray foreground run won't double up.

The plist itself is **not** in this repo — it ships in the
**[dotfiles repo](https://github.com/KiranM27/dotfiles)** as the `launchagents`
stow package (`launchagents/Library/LaunchAgents/sg.lexi.ctx-monitor.plist`).
dotfiles' `./index.sh` both symlinks it into `~/Library/LaunchAgents/` **and**
registers it via `scripts/load_launchagents.sh`. Edit the plist at its real path
in the dotfiles repo, not through the symlink.

#### Not running? Diagnose with `launchctl list`

    launchctl list | grep ctx-monitor

- **No output** — launchd has never heard of the job: the plist is *stowed but
  not registered*. Bootstrap it (above). The symlink existing tells you nothing.
- **PID + `0`** — healthy.
- **`-` for PID, or non-zero exit** — *registered but crashing*. Read
  `~/Library/Logs/ctx-monitor.err`; usually the `~/.claude/tools/ctx-monitor`
  symlink does not resolve.

A stale `.monitor.lock` is never the cause — the lock is an `flock`, released
automatically on process death. Do not delete it to "fix" a startup problem.

## Dashboard

One row per live pane: `#` (serial), PANE, NAME (the pane's `@cc_name` tmux
user option set by /tag; `-` when unset), DIR (repo dir name), SESSION
(first 8 chars), CONTEXT (used %), STATE. STATE shows readable labels —
watching (ARMED), checkpoint requested (SAVE_SENT), compacting
(COMPACT_SENT), reorienting (REORIENT_SENT), ERROR - needs attention; the
internal names are unchanged in `monitor.log` and `monitor-state.json`.
Cycle: ARMED -> SAVE_SENT -> COMPACT_SENT -> REORIENT_SENT -> ARMED. ERROR
(red) means the cycle failed twice; the monitor will never act on that pane
again until you restart it (a restart resets ERROR panes to ARMED) or the
pane gets a new session (/clear). Transitions and sends append to
`/tmp/claude-ctx/monitor.log`.

## Checkpoints

Written by the watched agent to `<cwd>/.cc-checkpoint-<sess8>.md` (sess8 =
first 8 chars of the session id). Safe to delete once a cycle completes.

## Helpers

- `cct [args...]` — launch claude in a solo tmux session named after the cwd
  (function sourced into zsh from `cct.sh`).
- `./adopt.sh <cwd> [<cwd>...]` — one `claude --resume` picker pane per cwd;
  2 panes = even-horizontal, 3-4 = tiled, 5th+ = new Ghostty window.

## Tests

    cd ~/.claude/tools/ctx-monitor && python3 -m unittest test_ctx_monitor -v

## Gotchas

- `BUSY_MARKER` in `ctx-monitor.py` is pinned to the installed Claude Code's
  running-turn indicator (e.g. "esc to interrupt"). Re-verify after CC
  upgrades: `tmux capture-pane -p -t <busy-pane>` and update the constant.
- A stale tap file does NOT mean a dead session — idle statuslines render
  sparsely. Liveness = pane existence; the monitor prunes on that alone.
- A pane in copy-mode (someone scrolled it) EATS send-keys — Claude Code
  never sees them. The monitor checks `#{pane_in_mode}` before every send
  and defers the action, mutating no state (logged once per streak), until
  the pane leaves the mode.
