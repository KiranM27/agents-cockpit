#!/usr/bin/env bash
# stamp-aerospace-wid.sh -- record which Ghostty window currently hosts a tmux
# session, so the `agents` cockpit can jump to it via Aerospace.
#
# Triggered by the tmux `client-attached` hook (see ~/.tmux.conf). When a client
# attaches, the currently-FOCUSED aerospace window is (by definition) the Ghostty
# window the client is in. We stamp that window-id onto the attaching session's
# active pane as @aerospace_wid. Re-stamps on every attach -> self-healing.
#
# Limitation: if a session is later moved to a different Ghostty window WITHOUT
# re-attaching, @aerospace_wid goes stale until the next attach. Acceptable v1.

set -euo pipefail

TMUX_BIN=$(command -v tmux || echo /opt/homebrew/bin/tmux)
AERO_BIN=$(command -v aerospace || echo /opt/homebrew/bin/aerospace)
[ -x "$TMUX_BIN" ] || exit 0
[ -x "$AERO_BIN" ] || exit 0

# The focused aerospace window-id == the Ghostty window the new client is in.
wid=$("$AERO_BIN" list-windows --focused --format '%{window-id}' 2>/dev/null | head -1 | tr -d '[:space:]')
[ -n "$wid" ] || exit 0

# Target the active pane of the session this client just attached to.
# Inside a client-attached run-shell, display-message resolves against the
# attaching client, so #{pane_id} is that client's active pane.
pane=$("$TMUX_BIN" display-message -p '#{pane_id}' 2>/dev/null | tr -d '[:space:]')
[ -n "$pane" ] || exit 0

"$TMUX_BIN" set-option -p -t "$pane" @aerospace_wid "$wid" 2>/dev/null || true
exit 0
