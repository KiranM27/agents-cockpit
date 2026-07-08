#!/bin/bash
# adopt.sh — migrate existing NON-tmux Claude Code windows into tmux panes so
# ctx-monitor can drive them (see DESIGN.md, Component 3).
#
# Usage: ./adopt.sh <cwd> [<cwd> ...]
#
# Builds tmux session(s) with one pane per cwd, each running `claude --resume`
# (interactive session picker). Layout follows the 2x2 rules: 2 panes =
# even-horizontal (side-by-side columns), 3-4 = tiled, the 5th+ cwd starts a
# NEW session in a NEW Ghostty window.
#
# Migration flow per old window: finish/exit claude in the old (non-tmux)
# window, pick that session in the picker pane created here, close the old
# window.
set -euo pipefail

TMUX_BIN="${TMUX_BIN:-/opt/homebrew/bin/tmux}"
CLAUDE_BIN="${CLAUDE_BIN:-/Users/kiran/.local/bin/claude}"

if [ $# -eq 0 ]; then
    echo "usage: $0 <cwd> [<cwd> ...]" >&2
    exit 1
fi

chunk=0
while [ $# -gt 0 ]; do
    chunk=$((chunk + 1))
    sess="adopt-$(date +%H%M%S)-$chunk"
    count=0
    for _slot in 1 2 3 4; do            # max 4 panes per session (2x2 rule)
        [ $# -eq 0 ] && break
        cwd=$1
        shift
        if [ ! -d "$cwd" ]; then
            echo "skip (not a directory): $cwd" >&2
            continue
        fi
        count=$((count + 1))
        if [ "$count" -eq 1 ]; then
            "$TMUX_BIN" new-session -d -s "$sess" -c "$cwd" "$CLAUDE_BIN --resume"
        else
            "$TMUX_BIN" split-window -t "$sess" -c "$cwd" "$CLAUDE_BIN --resume"
            if [ "$count" -eq 2 ]; then
                "$TMUX_BIN" select-layout -t "$sess" even-horizontal
            else
                "$TMUX_BIN" select-layout -t "$sess" tiled
            fi
        fi
    done
    if [ "$count" -gt 0 ]; then
        # -na forces a NEW Ghostty instance so --args/-e are honored even when
        # Ghostty is already running (~12s cold start is normal).
        open -na Ghostty --args -e "$TMUX_BIN" attach -t "$sess"
        echo "$sess: $count pane(s). In each pane, pick the session to migrate,"
        echo "then close its old (non-tmux) window."
    fi
done
