# agents cockpit

A **Textual TUI** over your ~20 Claude Code tmux sessions. One card per session:
an identity glyph + name + context %, with sessions that **need you** pushed to
the top in red. Arrow-key through the list, **Enter** to jump straight to the
Ghostty window that hosts the selected session (via Aerospace). A second
**Context** tab mirrors the ctx-monitor dashboard.

> **`agents` is the TUI. `agents-classic` is the original fzf picker**, preserved
> next to it. They share the same read-tmux / jump-via-Aerospace design; the
> difference that matters day-to-day is identity:
> - **TUI (`agents`)** takes each card's **name from the live Claude session
>   title** (`#{pane_title}`, what `/rename` sets) and auto-sends **`/color`** to
>   new windows it spawns. It does **not** read `@cc_name`/`@cc_color`.
> - **Classic (`agents-classic`)** reads `@cc_name`/`@cc_color` stamped by the
>   legacy **`/tag`** slash command.

> **Companion:** the shell / tmux / Aerospace / launchd / `~/.claude` glue this
> cockpit rides on lives in the **[dotfiles repo](https://github.com/KiranM27/dotfiles)**
> (the attention hook, the `client-attached` wid-stamp hook, the statusline tap,
> the ctx-monitor LaunchAgent). This repo is just the cockpit + ctx-monitor.

## The core idea: read tmux, jump via Aerospace

tmux owns the truth about sessions, panes, and per-pane state. Aerospace owns
window focus. The cockpit reads everything from tmux and uses one stamped value
(`@aerospace_wid`) to ask Aerospace to focus the right Ghostty window.

```
  tmux  ──reads──►  agents (TUI)  ──⏎──►  aerospace focus --window-id <wid>
   ▲                                                 ▲
   │ name = Claude title #{pane_title} (/rename)     │ @aerospace_wid stamped
   │ attention tint  bg=colour52  (attention hook)   │ on client-attached hook
   └─ ctx % from /tmp/claude-ctx/*.json (statusline) ┘
   └─ last message + age from ~/.claude/projects/*.jsonl transcripts
```

Every ~1.5s the TUI re-reads all of that on a background thread and repaints in
place — no server, no manual refresh (see [Refresh mechanism](#refresh-mechanism)).

## The pieces

1. **`agents`** — the TUI launcher (this dir; symlinked to `~/.local/bin/agents`).
   It self-locates through the symlink, runs the isolated venv's python, sets
   this Ghostty window's title to **`Agent Cockpit`** (OSC-2, so Aerospace can
   single it out), moves it to Aerospace **workspace D**, then execs
   `python -m agents_tui`. Only dependency: `textual`.

2. **`agents-classic`** — the original **fzf** picker (symlinked to
   `~/.local/bin/agents-classic`). Pure bash + fzf; reads `@cc_name`/`@cc_color`
   (set by `/tag`) for identity and uses fzf's HTTP server to live-reload. Kept
   as-is for anyone who prefers the one-line-per-session picker.

3. **`stamp-aerospace-wid.sh`** + a tmux `client-attached` hook — records which
   Ghostty window hosts each session so **Enter** can jump to it. See
   [How the wid stamp works](#how-the-wid-stamp-works).

4. **`ctx-monitor/`** — a companion stdlib-python daemon that drives unattended
   compaction. Shares the same `/tmp/claude-ctx` tap. See
   [ctx-monitor lives here too](#ctx-monitor-lives-here-too).

## Using the TUI

The top row carries the tab bar (**Agents** / **Context**) on the left and a live
stats strip on the right: `N agents · W working · A need attention · HH:MM:SS`.
**Tab** (or **←/→**) cycles between the two tabs; the tabs are also clickable.

### Agents tab

A two-pane view: the **list** on the left, a **preview** of the selected
session's transcript on the right. Rows are grouped into sections, top → bottom:

| Section       | Header       | Glyph |
|---------------|--------------|:-----:|
| needs-input   | `● needs you`| `●`   |
| working       | `⋮ running`  | `⋮`   |
| idle          | `inactive`   | `○`   |

`needs-you` sits at the top so anything waiting on you is impossible to miss.
(`working` rows animate a braille spinner in place of the `⋮`.)

**Row anatomy** (each card is 2–3 lines):

```
● template-fill-overhaul                 ctx 37% · high · 4m
   lexi-backend · fix/template-engine
   dim one-line snippet of the latest message…
```

- **Line 1** — the state glyph + the **headline** (the cleaned Claude session
  title, i.e. `/rename`'s value; falls back to `project · task`) + a
  right-aligned cluster `ctx NN% · <effort> · <age>`. A `⇄` leads the cluster
  when the row is pinned to a section (see **m**). The effort token is tinted to
  match the statusline (low=dim … xhigh=yellow, max=red).
- **Subtitle** — when the headline came from the Claude title, `project · task`
  drops to a dim line beneath it.
- **Snippet** — one dim line of the latest transcript message (the pending
  question, for a row that needs you).

The **preview** pane shows the selected session's title, a status chip, a
metadata line (`claude · cwd · pid · age`), a second cluster
(`model · effort · ctx% · 5h% · 7d% · wt`), then the rendered tail of the
transcript (assistant turns as markdown, your prompts in blue `❯`, injected
system turns in yellow `⚙`), plus a red "press ⏎ to jump" banner when it needs you.

### Context tab

Mirrors the ctx-monitor dashboard as cards. A liveness line at the top —
`monitor: alive (pid N)` or `monitor: NOT running` — derived from `pgrep` of the
live process (never the stale lock file). Columns:

```
PANE    NAME             PROJECT       SESSION   CTX%   STATE
```

`STATE` is the monitor's cycle stage — `watching`, `checkpoint requested`,
`compacting`, `reorienting`, or `ERROR - needs attention` (rendered red). Rows
are sorted by **CTX% descending** (most urgent first). **Enter** on an `ERROR`
row confirms and clears the monitor error (re-arms the watcher) — it does **not**
touch the running session.

### Keybindings

Grounded in `agents_tui/app.py` (`on_key` + `BINDINGS` + the footer bar):

| Key        | Action                                                            |
|------------|-------------------------------------------------------------------|
| **↑ / ↓**  | Move selection (Agents list, or Context rows)                     |
| **Enter ⏎**| Agents: jump to the session's Ghostty window (via Aerospace); offer to re-attach if it has no live window. Context: clear an `ERROR` row |
| **Tab**    | Cycle tabs (Agents ↔ Context)                                     |
| **← / →**  | Cycle tabs (→ next, ← previous)                                   |
| **r**      | Reply — type a message and send it into the selected session's pane |
| **a / A**  | Actions menu → **Rename** (`/rename`) · **Restart** (kill + reopen resuming the same conversation) |
| **n / N**  | New window — 3-step flow: name → directory → model → spawn        |
| **m / M**  | Move the selected session to another section (a pin; new activity clears it) |
| **/**      | Filter — fuzzy type-to-filter over name + label                  |
| **⌫ Backspace** | Kill the selected session (mandatory confirm; refuses the cockpit's own session) |
| **q / ^c** | Quit                                                             |
| **Esc**    | Cancel filter / dismiss the open modal                            |

## Set an identity

The TUI reads each session's **name from its Claude session title** (`/rename`).
When it spawns a new window it also auto-sends **`/color`** so each window gets a
distinct tab color for identification. So just use Claude Code's built-ins inside
any agent session:

```
/rename auth-fix
/color blue
```

(`/tag`, which stamps `@cc_name`/`@cc_color`, is the *legacy* path read only by
`agents-classic`, the fzf original. The TUI ignores it.)

## Refresh mechanism

The TUI is a plain Textual app with its own poll loop — **no HTTP server**. On
mount it schedules two intervals:

- **`set_interval(1.5s, refresh_data)`** — `refresh_data` is an
  `@work(thread=True, exclusive=True)` worker: it calls `data.gather_agents()`
  (tmux + ctx + transcript reads) off the UI thread and marshals the result back
  with `call_from_thread`, which repaints both tabs. Rows are updated **in place**
  by a stable key (session_id → session name) so refreshes never tear down and
  remount unchanged cards (no flicker, no scroll jump).
- **`set_interval(0.1s, _tick_spinner)`** — advances the braille "working"
  spinner ~10fps between the slower data ticks; only `working` rows repaint.

So attention / identity / ctx changes appear within ~1.5s without you touching
anything.

> *Classic only:* `agents-classic` instead uses **fzf's HTTP server**
> (`--listen-unsafe=127.0.0.1:<port>`) plus a background poller that
> `POST`s `reload(agents-classic --rows)` every 2s. `--listen-unsafe` (not plain
> `--listen`) is required because `reload` executes a process; it's bound to
> loopback and the reloaded command is a fixed literal. That mechanism is
> specific to the fzf picker and is gone in the TUI.

## Data sources

Everything the TUI shows is derived, per refresh, from sources that already exist:

- **tmux** — `list-sessions`, `list-panes -a`, the active pane's `#{pane_title}`
  (the Claude session title → card name), `@aerospace_wid` (the window stamp),
  and `window-active-style` (`bg=colour52` → "needs you").
- **`/tmp/claude-ctx/<session_id>.json`** — the statusline tap written by
  `~/.claude/statusline.sh`: `pct`, `cwd`, `ts`, plus `model`, `effort`,
  `worktree`, and the 5h / 7d rate-limit windows.
- **`~/.claude/projects/*/<session_id>.jsonl`** — transcript tail → the last
  message snippet + age (tail-read and cached by `(path, mtime)`; only the
  selected agent is deep-parsed for the preview).
- **`/tmp/claude-ctx/monitor-state.json`** + **`pgrep`** — the Context tab's per-
  session STATE and the monitor's liveness.

## How the wid stamp works

Pure tmux, no `settings.json` edit. A tmux hook:

```
set-hook -g client-attached 'run-shell "…/agents-cockpit/stamp-aerospace-wid.sh"'
```

fires on every client attach. At that moment the **focused** Aerospace window is
(by definition) the Ghostty window the client just attached into, so the script
reads `aerospace list-windows --focused --format '%{window-id}'` and stamps it as
`@aerospace_wid` on the attaching session's active pane. Because it re-runs on
every attach, it **self-heals** — re-attach and the mapping is fresh. Enter's
resolver prefers the stamped `@aerospace_wid`, and falls back to matching the
session's cleaned title against `aerospace list-windows --all` when the stamp is
missing or stale.

## ctx-monitor lives here too

`ctx-monitor/` is a companion stdlib-python daemon (no LLM, no deps) that watches
every tmux pane running Claude Code and, when a session's context crosses a
threshold, drives an unattended checkpoint cycle: save state → `/compact` →
reorient → continue. It's independent of the cockpit but shares the same
`/tmp/claude-ctx` tap that feeds the context %, and the **Context** tab mirrors
its dashboard. Details:
[`ctx-monitor/README.md`](ctx-monitor/README.md) and
[`ctx-monitor/DESIGN.md`](ctx-monitor/DESIGN.md).

## Install (fresh machine)

The `agents` launcher self-locates, so the repo can live anywhere.

```sh
# 1. Clone (any location)
git clone https://github.com/KiranM27/agents-cockpit
cd agents-cockpit

# 2. Create the isolated venv (only dep: textual)
/opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. Put the launchers + the aerospace-stamp hook script on PATH
ln -s "$PWD/agents"                 ~/.local/bin/agents
ln -s "$PWD/agents-classic"         ~/.local/bin/agents-classic
ln -s "$PWD/stamp-aerospace-wid.sh" ~/.local/bin/stamp-aerospace-wid.sh

# 4. Wire ctx-monitor into Claude (so ~/.claude/tools/ctx-monitor resolves here)
mkdir -p ~/.claude/tools && ln -s "$PWD/ctx-monitor" ~/.claude/tools/ctx-monitor
```

5. **Install the statusline tap** so the context % column populates — the block
   that publishes `/tmp/claude-ctx/<session_id>.json`. See
   [`ctx-monitor/README.md`](ctx-monitor/README.md#installing-the-statusline-tap).
6. **The tmux + `~/.claude` glue** the cockpit relies on lives in the
   **[dotfiles repo](https://github.com/KiranM27/dotfiles)** and `~/.claude`, not
   here: the `client-attached` hook that runs `stamp-aerospace-wid.sh` (needed
   for Enter → Aerospace jump; the hook invokes it via `~/.local/bin`, so step 3
   above is what wires it up), the attention hook (`tmux-attention.sh`), and the
   optional ctx-monitor LaunchAgent (the `launchagents` stow package). Follow the
   dotfiles README to set them up.

## Launch

```sh
agents          # launch the TUI cockpit
agents-classic  # the original fzf picker (Enter = jump to window)
```

## Limitations

- **Stale wid**: if a session is moved to a *different* Ghostty window without
  re-attaching, `@aerospace_wid` points at the old window until the next attach.
  Re-attach (or detach/attach) to fix. The title-match fallback covers most gaps.
- **Ctx staleness**: ctx % comes from files the statusline refreshes only on
  render (frequently mid-turn, sparsely when idle). A session whose statusline
  isn't running (or that isn't a CC session) shows `—`. A stale tap file does not
  mean the session is dead — pane existence is the liveness check.
- **Name resolution is per-pane**: the card name comes from the active pane's
  `#{pane_title}`. A session with no meaningful title (a bare `zsh`/`claude`/tmux
  value) falls back to a prettified `project · task` label — `/rename` it for a
  clean name.
