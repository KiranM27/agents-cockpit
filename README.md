# agents cockpit

An fzf picker over your ~20 Claude Code tmux sessions. One row per session:
an identity dot + name + context %, with sessions that **need attention**
pushed to the top in red. Press **Enter** to jump to the Ghostty window that
hosts the selected session.

## The core idea: read tmux, jump via Aerospace

tmux owns the truth about sessions, panes, and per-pane state. Aerospace owns
window focus. The cockpit reads everything from tmux and uses one stamped value
(`@aerospace_wid`) to ask Aerospace to focus the right Ghostty window.

```
  tmux  ──reads──►  agents (fzf)  ──Enter──►  aerospace focus --window-id <wid>
   ▲                                                   ▲
   │ stamps @cc_name/@cc_color (/tag)                  │ @aerospace_wid stamped
   │ tint bg=colour52 (attention hook)                 │ on client-attach hook
   └─ ctx % from /tmp/claude-ctx/*.json (statusline) ──┘
```

## The four pieces

1. **`agents`** — the cockpit script (this dir; symlinked to `~/.local/bin/agents`).
   For each tmux session it shows:
   - **Identity dot + name** — from pane-options `@cc_name` / `@cc_color` if the
     session was tagged (see `/tag`); otherwise a grey dot and a *prettified*
     session name (`cc-lexi-backend-34731` → `lexi-backend`).
   - **Context %** — joined from `/tmp/claude-ctx/<sid>.json`, which
     `~/.claude/statusline.sh` rewrites every ~2s. Each file has a `pane` (%NN)
     and `pct`; we map the session's panes → pct. Entries older than 60s are
     treated as stale and shown as `—`.
   - **Attention** — a session is "needy" if **any** of its panes has
     `window-active-style bg=colour52`, set by
     `~/.claude/hooks/tmux-attention.sh` when Claude Code wants you. Needy rows
     get a `❗` and go red, sorted to the top.

2. **`/tag <color> <name>`** — a Claude Code slash command
   (`~/.claude/commands/tag.md`). Run it *inside an agent* to give that session
   an identity. It stamps `@cc_name` and `@cc_color` on the current pane and
   renames the tmux session. `<name>` may contain spaces. Valid colors:
   `red blue green yellow purple orange pink cyan`.

3. **`stamp-aerospace-wid.sh`** + a tmux `client-attached` hook — records which
   Ghostty window hosts each session so Enter can jump to it. See below.

4. **This README.**

## Launch

```sh
agents              # interactive picker
agents --list       # print the rows once (no fzf) — for testing / piping
```

In the picker: **Enter** jumps to the session's Ghostty window (via Aerospace).
If a session has no window mapping yet, Enter prints a hint to attach it once.

## Set an identity

Inside any agent session:

```
/tag blue auth-fix
/tag orange drafting review pass 2
```

This colors the dot, labels the row, and renames the session.

## Refresh mechanism

The interactive picker uses **fzf's HTTP server** (`--listen-unsafe=127.0.0.1:<port>`)
plus a background poller that `POST`s `reload(agents --rows)` every 2 seconds, so
attention/identity/ctx changes appear within ~2s without you touching anything.

Why `--listen-unsafe` and not `--listen`? Verified against fzf 0.70: plain
`--listen` **refuses** actions that execute a process (`reload` runs a command).
`--listen-unsafe` is required. It's bound to `127.0.0.1` only and the reloaded
command is a fixed literal (`agents --rows`, never user input), so it's contained.
The initial list is populated via `--bind start:reload(agents --rows)`. If no
free port can be bound, the script falls back to a static (non-live) snapshot.

## How the wid stamp works (chosen approach)

Pure tmux, no `settings.json` edit. A tmux hook:

```
set-hook -g client-attached 'run-shell "…/agents-cockpit/stamp-aerospace-wid.sh"'
```

fires on every client attach. At that moment the **focused** Aerospace window is
(by definition) the Ghostty window the client just attached into, so the script
reads `aerospace list-windows --focused --format '%{window-id}'` and stamps it as
`@aerospace_wid` on the attaching session's active pane. Because it re-runs on
every attach, it **self-heals** — re-attach and the mapping is fresh.

## v1 limitations

- **Stale wid**: if a session is moved to a *different* Ghostty window without
  re-attaching, `@aerospace_wid` points at the old window until the next attach.
  Re-attach (or detach/attach) to fix.
- **Name prettify heuristic**: untagged sessions get their name by stripping a
  leading `cc-` and a trailing `-<digits>` (the launcher pid). Unusual session
  names may prettify oddly — tag them with `/tag` for a clean label.
- **Ctx staleness**: ctx % comes from files refreshed by the statusline every
  ~2s; entries older than 60s show `—`. A session whose statusline isn't running
  (or that isn't a CC session) shows `—`.
- **Identity is per-pane**: `@cc_name`/`@cc_color` live on the session's *active*
  pane. Splitting and switching the active pane within a session can drop the
  stamp from view until re-tagged. Fine for the one-pane-per-agent layout.
