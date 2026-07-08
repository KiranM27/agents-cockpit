# cct — launch claude inside a solo tmux session named after the cwd, so
# ad-hoc Claude Code windows are born monitorable by ctx-monitor.
# Sourced from ~/.zshrc (see DESIGN.md, Component 3). The attaching terminal
# is the window you run it from.
cct() {
    # Validate TMUX instead of trusting it: it can be inherited stale (e.g.
    # Ghostty launched from inside tmux), so only treat ourselves as in-tmux
    # if $TMUX_PANE really exists and its tty is OUR tty.
    if [ -n "$TMUX" ] && [ -n "$TMUX_PANE" ] && \
       [ "$(tmux display-message -p -t "$TMUX_PANE" '#{pane_tty}' 2>/dev/null)" = "$(tty)" ]; then
        # Already inside tmux: this pane is already monitorable.
        claude "$@"
        return
    fi
    local claude_bin base name cmd arg
    claude_bin=$(command -v claude) || {
        echo "cct: claude not found on PATH" >&2
        return 1
    }
    base=$(basename "$PWD" | tr -c 'a-zA-Z0-9_-' '-')
    base=${base%-}                  # tr maps basename's trailing newline to '-'
    name="cc-${base}-$$"
    cmd=$(printf '%q' "$claude_bin")
    for arg in "$@"; do
        cmd="$cmd $(printf '%q' "$arg")"
    done
    # Clear any stale inherited TMUX so new-session doesn't refuse to "nest".
    TMUX= TMUX_PANE= tmux new-session -s "$name" -c "$PWD" "$cmd"
}

# --- ctx-monitor command family (all sourced from ~/.zshrc via this file) ------
CTX_MONITOR_HOME="${CTX_MONITOR_HOME:-$HOME/.claude/tools/ctx-monitor}"

# ctx-monitor — START the watcher in the foreground (Ctrl+C stops it). Run this
# in a spare pane/window when you step away. Flags pass straight through, e.g.
#   ctx-monitor                 # defaults: save 35%, escape 40%, rearm 20%, 5s
#   ctx-monitor --dry-run       # watch the dashboard, send nothing
#   ctx-monitor --t1 30 --tick 3
ctx-monitor() { python3 "$CTX_MONITOR_HOME/ctx-monitor.py" "$@"; }

# cct-adopt — migrate ALREADY-OPEN (non-tmux) windows into tmux so the watcher
# can see them. Usage: cct-adopt <cwd> [<cwd> ...]  (pick the session in each
# pane's `claude --resume` picker). Distinct from `cct`, which starts a NEW one.
cct-adopt() { "$CTX_MONITOR_HOME/adopt.sh" "$@"; }
# ------------------------------------------------------------------------------
