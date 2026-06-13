"""app.py — the Textual UI for agents-tui.

Layout (see spec / mockup):
  ┌ header: ◆ agents tui ............ N agents · W working · A need attention · HH:MM:SS
  ├ left  (~48%) "agents"           │ right (~52%) "preview"
  │  ❯ filter…              m/n     │  <project · task>
  │  ● project · task        age    │  [● needs input] claude  cwd  pid · age ago
  │     dim snippet…                │  ❯ last prompt
  │  ○ project · task        age    │  assistant prose
  │     dim snippet…                │  ● Edit file (+a -b)
  │                                 │  ✓ commit line
  │                                 │  (red banner if needs input)
  └ footer: ↿⇂ move · ⏎ open window · → next alert · type filter · esc clear · ^c quit

Data gathering runs in a thread worker (@work(thread=True)) on a ~1.5s timer so
the UI thread stays responsive. Selection is keyed on session_id (falls back to
session name) so it survives refreshes even as rows reorder.
"""

from __future__ import annotations

import difflib
import os
import random
from datetime import datetime

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TabbedContent, TabPane, TextArea

from . import data
from .data import Agent

REFRESH_SECONDS = 1.5

# Catppuccin-Mocha-ish palette.
BG = "#1e1e2e"
ATTN = "#f38ba8"       # pink/red — attention
ACCENT = "#89b4fa"     # blue — prompts / headers / accents
SUCCESS = "#a6e3a1"    # green — success
DIM = "#6c7086"        # idle grey
BRIGHT = "#cdd6f4"     # active text
SELBG = "#3a1e2e"      # dark-maroon selection band
YELLOW = "#f9e2af"     # yellow (catppuccin) — xhigh effort / injected system msgs

# Section headers for the list: one per non-empty state group, top→bottom.
SECTION_LABELS = {
    "needs-input": "● needs you",
    "working":     "⋮ running",
    "idle":        "inactive",
}
SECTION_ORDER = ["needs-input", "working", "idle"]

GLYPH = {"needs-input": "●", "working": "⋮", "idle": "○"}
# Braille spinner frames for the animated "working" glyph (ticked ~10fps).
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# effort-level tint, mirroring the statusline (low=dim, medium=blue/cyan,
# high=green, xhigh=yellow, max=red). Unknown -> bright.
_EFFORT_COLOR = {
    "low": DIM,
    "medium": ACCENT,
    "high": SUCCESS,
    "xhigh": YELLOW,      # yellow (catppuccin)
    "max": ATTN,
}

MODELS = [("Opus 4.8", "claude-opus-4-8"), ("Sonnet 4.6", "claude-sonnet-4-6"),
          ("Haiku 4.5", "claude-haiku-4-5-20251001"), ("Fable 5", "claude-fable-5")]
SPAWN_COLORS = ["red", "blue", "yellow", "purple", "orange", "pink", "cyan"]  # NEVER "green" (reserved)


def _state_color(state: str) -> str:
    return {"needs-input": ATTN, "working": BRIGHT, "idle": DIM}[state]


def _effort_color(effort: str | None) -> str:
    return _EFFORT_COLOR.get(effort or "", BRIGHT)


def _pct_str(pct) -> str:
    """Format a 0-100 usage % as 'NN%', or '—' when absent."""
    if pct is None:
        return "—"
    try:
        return f"{int(round(float(pct)))}%"
    except (TypeError, ValueError):
        return "—"


class Header(Static):
    """Top-right stats strip: live counts + clock, right-aligned, sharing the
    tab row."""

    def update_counts(self, total: int, working: int, attention: int) -> None:
        right = Text()
        right.append(f"{total} agents", style=BRIGHT)
        right.append("  ·  ", style=DIM)
        right.append(f"{working} working", style=ACCENT)
        right.append("  ·  ", style=DIM)
        right.append(f"{attention} need attention",
                     style=ATTN if attention else DIM)
        right.append("  ·  ", style=DIM)
        right.append(datetime.now().strftime("%H:%M:%S"), style=DIM)
        self.update(right)


class AgentRow(Static):
    """One agent = two lines (title + snippet). Selection band via CSS class.

    `agent` is a reactive so an in-place reassignment on refresh re-invokes
    render() WITHOUT remounting the widget (the flicker fix — Issue 1). Widget
    identity persists across refreshes; only the rendered content changes.
    """

    # layout=True so a content reassignment re-renders + relayouts in place.
    agent: reactive[Agent | None] = reactive(None, layout=True)
    # Shared braille-spinner phase, advanced ~10fps by AgentsApp._tick_spinner.
    # A class var (not per-instance) so all working rows spin in lockstep.
    spin_frame: int = 0

    def __init__(self, agent: Agent, selected: bool) -> None:
        super().__init__()
        self.set_reactive(AgentRow.agent, agent)
        self._selected = selected

    def on_mount(self) -> None:
        # Apply the tagged height class on first mount (the initial agent is set
        # via set_reactive in __init__, which does NOT fire watch_agent).
        self._apply_tagged_class(self.agent)

    def watch_agent(self, agent: Agent | None) -> None:
        # Re-apply on every in-place reassignment (a row may gain/lose its tag).
        self._apply_tagged_class(agent)

    @staticmethod
    def _card_title(a: Agent) -> str:
        """Headline for the card. Precedence: cleaned pane_title (the live Claude
        session title) -> `project · task` label fallback."""
        return a.pane_title or a.label

    @staticmethod
    def _has_subtitle(a: Agent) -> bool:
        """True when the title came from pane_title (i.e. it's NOT the
        `project · task` label), so the label shows as a dim subtitle and
        the card needs the extra (3rd) content line."""
        return bool(a.pane_title)

    def _apply_tagged_class(self, agent: Agent | None) -> None:
        """Toggle the `.tagged` class so the CSS picks the right fixed height.

        A card with a SUBTITLE has 3 content lines (title + subtitle + snippet)
        and needs one extra row vs a plain 2-line card. The subtitle appears
        whenever the title came from a cleaned pane_title (most live sessions
        have a pane_title, so most cards are now 3-line). We use a
        FIXED height per class (not `height: auto`) on purpose: auto-height
        re-measures content width and makes the right-aligned/padded stat line
        wrap, inflating the card. Toggling on the reactive keeps this in lockstep
        with in-place updates — no remount, identity preserved.
        """
        self.set_class(bool(agent and self._has_subtitle(agent)), "tagged")

    def render(self) -> Text:  # type: ignore[override]
        a = self.agent
        if a is None:
            return Text("")
        color = _state_color(a.state)
        if a.state == "working":
            glyph = SPINNER_FRAMES[AgentRow.spin_frame % len(SPINNER_FRAMES)]
        else:
            glyph = GLYPH[a.state]
        title_style = f"bold {color}" if a.state != "idle" else color

        # self.size.width is the CONTENT width (already inside border+padding),
        # so right-align directly against it. A small safety margin avoids the
        # age/stat cluster clipping on the last column.
        width = max(self.size.width, 20)

        # TITLE (line 1): glyph + the agent's headline + a right-aligned
        # `ctx NN% · <effort> · <age>` cluster (the effort token is omitted when
        # effort is absent; ctx and age are always shown). Headline precedence:
        # cleaned pane_title (the live Claude session title) -> `project · task`.
        # When the headline is NOT the label, `project · task` drops to a dim
        # subtitle line below.
        #
        # The right cluster is built FIRST so we can RESERVE its exact cell width
        # and TRUNCATE the title with `…` rather than let it collide with the
        # cluster (mirrors the snippet-reservation pattern used on line 3).
        title_text = self._card_title(a)
        age = a.age_str
        cluster1 = Text()
        ctx = f"{a.pct}%" if a.pct is not None else "—"
        cluster1.append(f"ctx {ctx}", style=DIM)
        cluster1.append(" · ", style=DIM)
        if a.effort:
            cluster1.append(a.effort, style=_effort_color(a.effort))
            cluster1.append(" · ", style=DIM)
        cluster1.append(age, style=DIM)

        line1 = Text()
        line1.append(f"{glyph} ", style=color)
        # budget for the title = width - glyph(2) - cluster - >=1 gap col.
        title_budget = width - 2 - cluster1.cell_len - 1
        if title_budget < 1:
            title_budget = 1
        if len(title_text) > title_budget:
            title_text = title_text[: max(0, title_budget - 1)] + "…"
        line1.append(title_text, style=title_style)

        gap = width - line1.cell_len - cluster1.cell_len
        if gap < 1:
            gap = 1
        line1.append(" " * gap)
        line1.append_text(cluster1)

        # SUBTITLE (when the title is a pane_title): dim
        # `project · task` under the headline.
        subtitle: Text | None = None
        if self._has_subtitle(a):
            subtitle = Text()
            subtitle.append("   ")
            subtitle.append(a.label, style=DIM)

        # SNIPPET line: ONE line of the latest message across the FULL card width
        # (ctx moved up to line 1's cluster). 3-col indent aligns it under the title.
        snip_line = Text()
        snip_line.append("   ")
        snip = (a.snippet or "").replace("\n", " ") or "—"
        snip_budget = width - 3
        if snip_budget < 1:
            snip_budget = 1
        if len(snip) > snip_budget:
            snip = snip[: max(0, snip_budget - 1)] + "…"
        snip_line.append(snip, style=DIM)

        out = Text()
        out.append_text(line1)
        if subtitle is not None:
            out.append("\n")
            out.append_text(subtitle)
        out.append("\n")
        out.append_text(snip_line)
        return out


class PreviewPane(VerticalScroll):
    """Right pane: rich rendered tail of the selected agent's transcript.

    IN-PLACE update (Issue 1 — the flicker fix): instead of tearing down and
    re-mounting every Static each show(), keep STABLE child widgets and call
    `.update()` on them. The fixed-structure header (title / chip / meta /
    spacer) is four persistent Statics. The variable-length transcript body is a
    POOL of Statics: each show() updates the existing ones in place and only
    mounts/removes the DELTA in count. So when the same agent is shown across
    consecutive ticks with the same event count, every body Static identity
    persists — no remount.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Fixed-structure header widgets (created lazily on first show()).
        self._w_title: Static | None = None
        self._w_chip: Static | None = None
        self._w_meta: Static | None = None
        self._w_spacer: Static | None = None
        # Pool of body Statics (transcript events / placeholders / banner).
        self._body_pool: list[Static] = []
        self._header_mounted = False
        # The key of the agent currently shown — used to auto-scroll to the
        # latest turn ONLY when the SELECTED AGENT CHANGES, never on a
        # steady-state content refresh of the same agent (Change 1: don't fight
        # the user's manual scroll on every 1.5s tick).
        self._shown_key: str | None = None

    def _ensure_header(self) -> None:
        if self._header_mounted:
            return
        self._w_title = Static()
        self._w_chip = Static()
        self._w_meta = Static()
        self._w_spacer = Static(Text(""))
        self.mount(self._w_title, self._w_chip, self._w_meta, self._w_spacer)
        self._header_mounted = True

    def _set_body(self, lines: list[Text]) -> None:
        """Update body Statics in place; mount/remove only the count delta."""
        # update existing pooled widgets in place
        for i, txt in enumerate(lines):
            if i < len(self._body_pool):
                self._body_pool[i].update(txt)
            else:
                w = Static(txt)
                self._body_pool.append(w)
                self.mount(w)
        # remove surplus widgets (count shrank)
        while len(self._body_pool) > len(lines):
            self._body_pool.pop().remove()

    def show(self, agent: Agent | None) -> None:
        self._ensure_header()

        # Did the SELECTED AGENT change since the last show()? If so we'll snap
        # to the newest turn after rendering; if it's the SAME agent (a 1.5s
        # steady-state content refresh) we leave the scroll where the user put
        # it.
        new_key = (agent.session_id or agent.session) if agent else None
        agent_changed = new_key != self._shown_key
        self._shown_key = new_key

        if agent is None:
            self._w_title.update(Text("no agent selected", style=DIM))
            self._w_chip.update(Text(""))
            self._w_meta.update(Text(""))
            self._set_body([])
            return

        # Big title
        title = Text()
        title.append(agent.label, style=f"bold {BRIGHT}")
        self._w_title.update(title)

        # Status chip + meta
        chip = Text()
        if agent.state == "needs-input":
            chip.append(" ● needs input ", style=f"bold {BG} on {ATTN}")
        elif agent.state == "working":
            chip.append(" working ", style=f"bold {BG} on {ACCENT}")
        else:
            chip.append(" idle ", style=f"{BRIGHT} on {DIM}")
        chip.append("  ")
        meta = []
        meta.append("claude")
        if agent.cwd:
            meta.append(agent.cwd)
        if agent.pid:
            meta.append(f"pid {agent.pid}")
        chip.append("  ".join(meta), style=DIM)
        chip.append(f" · {agent.age_str} ago", style=DIM)
        self._w_chip.update(chip)

        # Second meta line: the full per-session metadata cluster. Each value
        # degrades to '—' when absent (old tap / pre-API-call session).
        self._w_meta.update(self._meta_cluster(agent))

        # Body: rendered transcript events
        events = []
        tpath = data.find_transcript(agent.session_id)
        if tpath:
            try:
                events = data.parse_transcript_preview(tpath)
            except Exception:
                events = []

        body: list[Text] = []
        if not events:
            body.append(Text("(no transcript activity to show)", style=DIM))
        else:
            for ev in events:
                body.append(self._render_event(ev))

        # Red banner at the bottom when needs input
        if agent.state == "needs-input":
            body.append(Text(""))
            banner = Text()
            banner.append(
                " waiting for your response — press ⏎ to jump to this window ",
                style=f"bold {BG} on {ATTN}",
            )
            body.append(banner)

        self._set_body(body)

        # Auto-scroll to the newest turn ONLY when the selected agent just
        # changed (fresh selection). On same-agent refresh ticks we deliberately
        # do NOT scroll, so the user's manual scrollback is never yanked.
        if agent_changed:
            try:
                self.scroll_end(animate=False)
            except Exception:
                pass

    @staticmethod
    def _meta_cluster(agent: Agent) -> Text:
        """Dim metadata cluster: model · effort · ctx% · 5h% · 7d% · wt.

        Color-tints the effort token (matches the statusline). Absent values
        render as '—'. 5h/7d append '(resets in …)' when a reset is known.
        """
        sep = "  ·  "
        t = Text()
        t.append(agent.model or "—", style=DIM)

        t.append(sep, style=DIM)
        t.append("effort ", style=DIM)
        if agent.effort:
            t.append(agent.effort, style=_effort_color(agent.effort))
        else:
            t.append("—", style=DIM)

        t.append(sep, style=DIM)
        ctx = f"{agent.pct}%" if agent.pct is not None else "—"
        t.append(f"ctx {ctx}", style=DIM)

        t.append(sep, style=DIM)
        t.append(f"5h {_pct_str(agent.five_h_pct)}", style=DIM)
        if agent.five_h_pct is not None:
            r = agent.five_h_resets_in
            if r:
                t.append(f" (resets in {r})", style=DIM)

        t.append(sep, style=DIM)
        t.append(f"7d {_pct_str(agent.seven_d_pct)}", style=DIM)
        if agent.seven_d_pct is not None:
            r = agent.seven_d_resets_in
            if r:
                t.append(f" (resets in {r})", style=DIM)

        if agent.worktree:
            t.append(sep, style=DIM)
            t.append(f"wt:{agent.worktree}", style=DIM)
        return t

    @staticmethod
    def _render_event(ev: dict):
        """Render ONE dialogue turn for the preview pane.

        ASSISTANT turns are rendered as MARKDOWN — the model's output IS markdown
        (bold/italic, inline code, fenced code blocks with highlighting, lists,
        headings), and showing raw `**`/backticks looked bad. USER turns stay blue
        with the ❯ marker (the 'this is me' signal); SYSTEM/injected turns stay
        yellow with ⚙ (verbose XML noise — markdown won't help). Each turn gets a
        trailing blank line so consecutive turns read as separated blocks.

        Returns a Rich renderable (Text or Group). PreviewPane's pooled Static
        widgets accept any renderable via .update(), so the in-place no-remount
        update path is preserved.
        """
        kind = ev["kind"]
        txt = ev["text"]
        if kind == "assistant":
            # Group the rendered markdown with a trailing blank line for spacing.
            return Group(Markdown(txt), Text(""))
        t = Text()
        if kind == "system":
            t.append("⚙ ", style=f"bold {YELLOW}")
            t.append(txt, style=YELLOW)
        else:  # user
            t.append("❯ ", style=f"bold {ACCENT}")
            t.append(txt, style=ACCENT)
        t.append("\n")
        return t


class ContextRowWidget(Static):
    """One Context-tab row = a single monospace line mirroring the monitor's
    columns (PANE NAME DIR SESSION CONTEXT STATE — the cosmetic serial `#` is
    dropped since it isn't known per-widget). Selection band via CSS class.

    `row` is a reactive `data.ContextRow` so an in-place reassignment on refresh
    re-invokes render() WITHOUT remounting the widget (the flicker fix — mirrors
    AgentRow). ERROR rows render RED, mirroring the monitor's ERROR tint.
    """

    # layout=True so a content reassignment re-renders + relayouts in place.
    row: reactive = reactive(None, layout=True)

    def __init__(self, row, selected: bool) -> None:
        super().__init__()
        self.set_reactive(ContextRowWidget.row, row)
        self._selected = selected

    def render(self) -> Text:  # type: ignore[override]
        r = self.row
        if r is None:
            return Text("")
        # Column widths mirror the monitor's render_lines(): PANE 6, NAME 18,
        # DIR 20, SESSION 10, CONTEXT right-aligned, then STATE.
        pane = (r.pane or "-")[:6]
        name = (r.name or "-")[:18]
        dir_ = (r.dir or "-")[:20]
        sess = (r.session8 or "-")[:10]
        ctx = f"{r.pct}%" if r.pct is not None else "—"
        line = "{:<6} {:<18} {:<20} {:<10} {:>5}  {}".format(
            pane, name, dir_, sess, ctx, r.state_label
        )
        if r.is_error:
            return Text(line, style=f"bold {ATTN}")
        return Text(line, style=BRIGHT)


class ContextPane(VerticalScroll):
    """Context tab: a per-row widget list mirroring the ctx-monitor dashboard.

    Columns match ctx-monitor.py's render_lines(): PANE, NAME, DIR, SESSION
    (session8), CONTEXT (used %), STATE (the cosmetic serial `#` is dropped). A
    liveness line at the top reports whether the monitor sidecar is running
    (from monitor.lock's PID). ERROR rows render RED, mirroring the monitor.
    Rows arrive pre-sorted by CONTEXT % descending from gather_context_rows().

    KEYED no-remount discipline (mirrors AgentsApp._sync_rows): the liveness
    Static is the persistent first child; each ContextRow is a keyed
    ContextRowWidget updated in place, with only the count/order DELTA
    mounted/removed/moved on refresh.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._w_liveness: Static | None = None
        # key -> stable ContextRowWidget; ordered keys for the diff.
        self._row_widgets: dict[str, ContextRowWidget] = {}
        self._ordered_keys: list[str] = []

    def _ensure_liveness(self) -> None:
        if self._w_liveness is not None:
            return
        self._w_liveness = Static()
        self.mount(self._w_liveness)

    @staticmethod
    def _row_key(r) -> str:
        """Stable key for a ContextRow: prefer the raw sid, then session8, then
        pane. Must match AgentsApp's _ctx_rows keying."""
        return r.sid or r.session8 or r.pane or "-"

    def update_rows(self, rows: list, liveness) -> None:
        """Re-render the liveness line + the per-row widgets from prepared data
        (all file-reading already done in data.py — pure presentation)."""
        self._ensure_liveness()

        live = Text()
        if liveness.alive:
            live.append("monitor: ", style=DIM)
            live.append("alive", style=f"bold {SUCCESS}")
            live.append(f" (pid {liveness.pid})", style=DIM)
        else:
            live.append("monitor: ", style=DIM)
            live.append("NOT running", style=f"bold {ATTN}")
        self._w_liveness.update(live)

        desired_keys = [self._row_key(r) for r in rows]
        by_key = {k: r for k, r in zip(desired_keys, rows)}
        desired_set = set(desired_keys)

        # 1. remove vanished rows
        for key in list(self._row_widgets):
            if key not in desired_set:
                self._row_widgets.pop(key).remove()

        # 2. update existing rows in place; mount new ones (order fixed in 3).
        for key in desired_keys:
            r = by_key[key]
            w = self._row_widgets.get(key)
            if w is None:
                w = ContextRowWidget(r, False)
                self._row_widgets[key] = w
                self.mount(w)
            else:
                w.row = r  # reactive(layout=True) -> in-place re-render

        # 3. reorder mounted widgets WITHOUT remounting (liveness stays first).
        try:
            current = [c for c in self.children
                       if isinstance(c, ContextRowWidget)]
            current_keys = [self._row_key(c.row) for c in current
                            if c.row is not None]
            if current_keys != desired_keys:
                prev = None
                for key in desired_keys:
                    w = self._row_widgets[key]
                    if prev is None:
                        # place after the liveness Static (first child)
                        self.move_child(w, after=self._w_liveness)
                    else:
                        self.move_child(w, after=prev)
                    prev = w
        except Exception:
            pass

        self._ordered_keys = desired_keys

    def set_row_state(self, key: str, state_label: str, is_error: bool) -> None:
        """Optimistically repaint a single row's displayed state (instant
        feedback after a reset, before the next refresh re-reads ground truth).
        Mutates the dataclass in place (not frozen) and refreshes the widget."""
        w = self._row_widgets.get(key)
        if w is None or w.row is None:
            return
        w.row.state_label = state_label
        w.row.is_error = is_error
        w.refresh(layout=True)


class KillConfirmScreen(ModalScreen):
    """Mandatory confirmation modal for the DESTRUCTIVE kill-session action.

    Shows BOTH the human display name AND the raw tmux session name (so the
    wrong session can't be killed by mistake). DEFAULT IS CANCEL: `esc`/`n`/
    dismissing all cancel; ONLY an explicit `y` confirms. We deliberately do
    NOT use a default-focused "Kill" button or accept a bare Enter as confirm —
    a stray Enter must never kill a session.

    Returns (via dismiss) True to proceed with the kill, False to cancel. While
    this screen is on the stack it captures key focus, so the main app's
    on_key filter/nav handling is paused behind it.
    """

    # `y` confirms; n cancels. Declared as priority so they're handled before
    # any focused-widget default. `escape` is handled by the App's priority
    # `escape` binding (action_clear_filter), which detects this modal is open
    # and dismisses it. Textual checks priority bindings App-first (see
    # App._check_bindings: `reversed(screen._binding_chain)`), so the App's
    # escape wins over a modal-level one — we therefore route escape through the
    # App, NOT a binding here. Enter is intentionally NOT bound to confirm — a
    # stray Enter must not kill.
    BINDINGS = [
        Binding("y", "confirm", "kill", priority=True),
        Binding("n", "cancel", "cancel", priority=True),
    ]

    def __init__(self, display_name: str, tmux_session: str) -> None:
        super().__init__()
        self._display_name = display_name
        self._tmux_session = tmux_session

    def compose(self) -> ComposeResult:
        body = Text()
        body.append("Kill ", style=f"bold {BRIGHT}")
        body.append(f"'{self._display_name}'", style=f"bold {ATTN}")
        body.append("?", style=f"bold {BRIGHT}")
        body.append("\n\n")
        body.append("This ends the Claude session in tmux session ", style=DIM)
        body.append(f"'{self._tmux_session}'", style=f"bold {ATTN}")
        body.append(".", style=DIM)
        body.append("\n\n")
        body.append(" [y] kill ", style=f"bold {BG} on {ATTN}")
        body.append("    ", style=DIM)
        body.append("[n / esc] cancel", style=DIM)
        with Vertical(id="killdialog"):
            yield Static(body, id="killbody")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_key(self, event) -> None:
        # `escape` cancel is routed via the App's priority `escape` binding
        # (action_clear_filter), which detects this modal is open and dismisses
        # it — App priority bindings are checked before this screen's on_key, so
        # we must NOT also dismiss here (double-dismiss would error). Just let
        # escape pass through to that handler.
        if event.key == "escape":
            return
        # y / n are handled by the priority bindings above; let those fire.
        if event.key in ("y", "n"):
            return
        # Belt-and-suspenders: swallow EVERY other key so nothing leaks to the
        # app behind the modal and so a stray Enter can never confirm.
        event.stop()


class ConfirmScreen(ModalScreen):
    """Generic yes/no confirm modal (NON-destructive — ACCENT border, not the
    red kill border). Models KillConfirmScreen's key discipline: `y` confirms,
    `n` cancels (both priority bindings), `escape` is routed via the App's
    priority escape binding, and every other key is swallowed so nothing leaks
    to the background and a stray Enter can never confirm. Default is CANCEL.

    Returns (via dismiss) True to proceed, False to cancel.
    """

    BINDINGS = [
        Binding("y", "confirm", "yes", priority=True),
        Binding("n", "cancel", "cancel", priority=True),
    ]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        body = Text()
        body.append(self._title, style=f"bold {ACCENT}")
        body.append("\n\n")
        body.append(self._body, style=BRIGHT)
        body.append("\n\n")
        body.append(" [y] yes ", style=f"bold {BG} on {ACCENT}")
        body.append("    ", style=DIM)
        body.append("[n / esc] cancel", style=DIM)
        with Vertical(id="confirmdialog"):
            yield Static(body, id="confirmbody")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_key(self, event) -> None:
        # `escape` cancel is routed via the App's priority escape binding
        # (action_clear_filter), checked before this screen's on_key — let it
        # pass (don't double-dismiss). y / n fire via the priority bindings.
        if event.key == "escape":
            return
        if event.key in ("y", "n"):
            return
        # Swallow everything else so nothing leaks to the app behind the modal.
        event.stop()


class ReplyTextArea(TextArea):
    """A TextArea whose ENTER submits instead of inserting a newline.

    A vanilla TextArea consumes `enter` in its own `_on_key` (inserting "\\n"
    and calling event.stop()), so the key never bubbles to the screen. We
    override `_on_key` to remap the submit/newline semantics for the reply box:
      - `enter`        -> POST a Submitted message (the screen sends + dismisses)
                          and do NOT insert a newline.
      - `shift+enter`  -> insert a literal newline (compose a multi-line draft).
      - everything else -> default TextArea behavior (normal editing).
    `escape` is left to the App's priority `escape` binding (which cancels the
    reply modal), so we deliberately don't touch it here.
    """

    class Submitted(Message):
        """Posted when the user presses Enter to send the reply."""

    async def _on_key(self, event) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.post_message(self.Submitted())
            return
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            self.insert("\n")
            self.scroll_cursor_visible(animate=False)
            return
        await super()._on_key(event)


class ReplyScreen(ModalScreen):
    """Inline REPLY overlay: type a message and send it to the selected agent's
    tmux pane (the running Claude session).

    Look mirrors KillConfirmScreen — dimmed backdrop, a centered card — but with
    an ACCENT (blue) border (this is a normal, non-destructive action). Header
    line `Reply to <name>`, then a multi-line ReplyTextArea (focused on open).

    KEY HANDLING (via ReplyTextArea, because a vanilla TextArea consumes Enter
    before it can bubble to the screen):
      - `enter`        -> ReplyTextArea posts Submitted -> SEND the draft +
                          dismiss.
      - `shift+enter`  -> ReplyTextArea inserts a literal newline (multi-line).
      - `escape`       -> the App's priority `escape` binding dismisses this
                          modal (cancel without sending). Textual resolves
                          priority bindings App-first, so the App's escape owns
                          modal dismissal; routing it there (not a modal-level
                          binding) avoids a double-dismiss.

    The actual tmux send lives in data.send_message_to_pane (stdlib-only); this
    screen only collects the text, calls it, toasts the result, and dismisses.
    """

    def __init__(self, display_name: str, pane: str) -> None:
        super().__init__()
        self._display_name = display_name
        self._pane = pane

    def compose(self) -> ComposeResult:
        header = Text()
        header.append("Reply to ", style=f"bold {BRIGHT}")
        header.append(self._display_name, style=f"bold {ACCENT}")
        hint = Text()
        hint.append("⏎ send", style=DIM)
        hint.append("   ·   ", style=DIM)
        hint.append("⇧⏎ newline", style=DIM)
        hint.append("   ·   ", style=DIM)
        hint.append("esc cancel", style=DIM)
        with Vertical(id="replydialog"):
            yield Static(header, id="replyheader")
            yield ReplyTextArea(id="replyinput")
            yield Static(hint, id="replyhint")

    def on_mount(self) -> None:
        self.query_one("#replyinput", ReplyTextArea).focus()

    def on_reply_text_area_submitted(self, event: ReplyTextArea.Submitted) -> None:
        event.stop()
        self._send()

    def _send(self) -> None:
        text = self.query_one("#replyinput", ReplyTextArea).text
        if not text.strip():
            # Nothing to send — just cancel rather than firing an empty Enter.
            self.dismiss(False)
            return
        ok, err = data.send_message_to_pane(self._pane, text)
        # Defer the toast to the App so it shows after this screen dismisses.
        app = self.app
        name = self._display_name
        if ok:
            app.call_after_refresh(lambda: app.notify(f"sent to {name}", timeout=3))
        else:
            app.call_after_refresh(
                lambda: app.notify(f"send failed: {err}", severity="error",
                                   timeout=4))
        self.dismiss(ok)




class _PickerInput(Input):
    async def _on_key(self, event) -> None:
        if event.key in ("up", "down", "enter"):
            event.stop()
            event.prevent_default()
            self.screen.on_picker_key(event.key)
            return
        await super()._on_key(event)


class NameInputScreen(ModalScreen):
    """Step 1 of the new-agent flow: enter a name for the new agent."""

    def __init__(self) -> None:
        super().__init__()

    def compose(self) -> ComposeResult:
        header = Text()
        header.append("New agent — name", style=f"bold {ACCENT}")
        hint = Text("⏎ next · esc cancel", style=DIM)
        with Vertical(id="namedialog"):
            yield Static(header, id="nameheader")
            yield Input(id="nameinput", placeholder="agent name…")
            yield Static(hint, id="namehint")

    def on_mount(self) -> None:
        self.query_one("#nameinput", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = self.query_one("#nameinput", Input).value.strip()
        if name:
            self.dismiss(name)


class DirPickerScreen(ModalScreen):
    """Step 2 of the new-agent flow: pick a working directory."""

    def __init__(self, dirs: list[str]) -> None:
        super().__init__()
        self._all_dirs = dirs
        self._filtered = dirs[:]
        self._sel = 0

    def compose(self) -> ComposeResult:
        header = Text()
        header.append("New agent — directory", style=f"bold {ACCENT}")
        hint = Text("↑/↓ move · ⏎ pick · esc cancel", style=DIM)
        with Vertical(id="dirdialog"):
            yield Static(header, id="dirheader")
            yield _PickerInput(id="dirfilter", placeholder="filter or type a path…")
            yield Static("", id="dirresults")
            yield Static(hint, id="dirhint")

    def on_mount(self) -> None:
        self.query_one("#dirfilter", _PickerInput).focus()
        self._render_results()

    def _render_results(self) -> None:
        """Render the filtered directory list with selection highlight."""
        from rich.text import Text as RText
        t = RText()
        dirs = self._filtered
        if not dirs:
            t.append("(no matching directories)", style=DIM)
            self.query_one("#dirresults", Static).update(t)
            return
        # Window up to 12 dirs around the selected index
        sel = self._sel
        total = len(dirs)
        if sel < 0:
            sel = 0
        if sel >= total:
            sel = total - 1
        self._sel = sel
        start = max(0, sel - 5)
        end = min(total, start + 12)
        start = max(0, end - 12)
        for i in range(start, end):
            d = dirs[i]
            base = os.path.basename(d.rstrip('/')) or d
            if i == sel:
                t.append("❯ ", style=f"bold {ACCENT}")
                t.append(base, style=f"bold {BRIGHT}")
                t.append(f"  {d}", style=DIM)
            else:
                t.append("  ", style=DIM)
                t.append(base, style=DIM)
                t.append(f"  {d}", style=DIM)
            if i < end - 1:
                t.append("\n")
        self.query_one("#dirresults", Static).update(t)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "dirfilter":
            return
        q = event.value.strip().lower()
        if not q:
            self._filtered = self._all_dirs[:]
        else:
            result = []
            for d in self._all_dirs:
                hay = d.lower()
                if q in hay:
                    result.append(d)
                elif difflib.SequenceMatcher(None, q, hay).ratio() > 0.55:
                    result.append(d)
                else:
                    words = hay.replace('/', ' ').replace('-', ' ').replace('_', ' ').split()
                    if any(difflib.SequenceMatcher(None, q, w).ratio() > 0.7 for w in words):
                        result.append(d)
            self._filtered = result
        self._sel = 0
        self._render_results()

    def on_picker_key(self, key: str) -> None:
        if key == "down":
            if self._filtered:
                self._sel = (self._sel + 1) % len(self._filtered)
            self._render_results()
        elif key == "up":
            if self._filtered:
                self._sel = (self._sel - 1) % len(self._filtered)
            self._render_results()
        elif key == "enter":
            if self._filtered:
                self.dismiss(self._filtered[self._sel])
            else:
                filter_text = self.query_one("#dirfilter", _PickerInput).value.strip()
                if filter_text:
                    expanded = os.path.expanduser(filter_text)
                    if os.path.isdir(expanded):
                        self.dismiss(expanded)


class ModelPickerScreen(ModalScreen):
    """Step 3 of the new-agent flow: pick which Claude model to use."""

    def __init__(self) -> None:
        super().__init__()
        self._sel = 0

    def compose(self) -> ComposeResult:
        header = Text()
        header.append("New agent — model", style=f"bold {ACCENT}")
        hint = Text("↑/↓ move · ⏎ spawn · esc cancel", style=DIM)
        with Vertical(id="modeldialog"):
            yield Static(header, id="modelheader")
            yield Static("", id="modelresults")
            yield Static(hint, id="modelhint")

    def on_mount(self) -> None:
        self._render_results()

    def _render_results(self) -> None:
        from rich.text import Text as RText
        t = RText()
        for i, (display, _model_id) in enumerate(MODELS):
            if i == self._sel:
                t.append("❯ ", style=f"bold {ACCENT}")
                t.append(display, style=f"bold {BRIGHT}")
            else:
                t.append("  ", style=DIM)
                t.append(display, style=DIM)
            if i < len(MODELS) - 1:
                t.append("\n")
        self.query_one("#modelresults", Static).update(t)

    def on_key(self, event) -> None:
        key = event.key
        if key == "down":
            self._sel = (self._sel + 1) % len(MODELS)
            self._render_results()
            event.stop()
        elif key == "up":
            self._sel = (self._sel - 1) % len(MODELS)
            self._render_results()
            event.stop()
        elif key == "enter":
            self.dismiss(MODELS[self._sel][1])
            event.stop()
        elif key == "escape":
            return  # let App's priority escape binding dismiss

class AgentsApp(App):
    """The agents-tui application."""

    CSS = f"""
    Screen {{ background: {BG}; }}

    /* Stats strip: docked to the RIGHT edge of the top row (height:1 keeps it
       a single-row strip, not a vertical column) so it shares the SAME line as
       the TabbedContent tab bar — left-aligned tabs ("Agents"/"Context") on
       the left, stats flush right. Docking keeps it out of normal flow, so the
       tab body still starts on the next row (no title row pushing it down). */
    #topbar {{
        dock: right;
        layer: overlay;
        width: auto;
        height: 1;
        padding: 0 1;
        background: {BG};
        color: {BRIGHT};
    }}

    #body {{ height: 1fr; }}

    /* Tabs: keep the cockpit's dark bg; let the tab panes fill the space. */
    TabbedContent {{ height: 1fr; background: {BG}; }}
    TabPane {{ padding: 0; background: {BG}; }}
    Tabs {{ background: {BG}; }}
    Tab {{ color: {DIM}; }}
    Tabs:focus .-active {{ color: white; }}

    #context {{
        height: 1fr;
        padding: 0 1;
        border: round {DIM} 50%;
        scrollbar-size-vertical: 1;
        scrollbar-color: #9399b2;
        scrollbar-color-hover: #b4befe;
        scrollbar-color-active: #b4befe;
        scrollbar-background: #181825;
        scrollbar-background-hover: #181825;
        scrollbar-background-active: #181825;
    }}

    /* Context-tab rows: CARDS mirroring AgentRow so selecting highlights the
       WHOLE card with ZERO geometry change. The base rule ALWAYS reserves a
       rounded border (round border = +2 rows), so selection only swaps the
       border COLOR — no reflow. One content line + 2 border rows -> FIXED
       height 3. We avoid `height: auto` for the same reason as AgentRow: auto
       re-measures and wraps the padded/aligned line, inflating the card.
       Overflow hidden so a padded line can never spill/wrap visibly. */
    ContextRowWidget {{
        height: 3;
        padding: 0 1;
        margin: 0 0 1 0;
        border: round {DIM} 40%;
        overflow: hidden;
    }}
    ContextRowWidget.selected {{
        background: {SELBG};
        border: round {ACCENT};
        border-left: thick {ACCENT};
    }}
    ContextRowWidget.selected-attn {{
        background: {SELBG};
        border: round {ATTN};
        border-left: thick {ATTN};
    }}

    #left {{
        width: 48%;
        border: round {DIM} 50%;
        padding: 0 1;
    }}
    #right {{
        width: 1fr;
        border: round {DIM} 50%;
        padding: 0 1;
    }}

    .panelabel {{ color: {ACCENT}; text-style: bold; height: 1; }}

    #filterrow {{ height: 1; }}
    #filter {{ width: 1fr; color: {DIM}; }}
    /* The filter Input is hidden (display:none) in command mode and only
       revealed while in filter mode (entered with `/`). When shown it replaces
       the #filter hint label in the row. A compact, borderless single-line. */
    #filterinput {{
        width: 1fr;
        height: 1;
        padding: 0;
        border: none;
        background: {BG};
        color: {BRIGHT};
        display: none;
    }}
    #filterinput.filtering {{ display: block; }}
    #filter.filtering {{ display: none; }}
    #matchcount {{ width: auto; color: {DIM}; content-align: right middle; }}

    #agentlist {{
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-color: #9399b2;
        scrollbar-color-hover: #b4befe;
        scrollbar-color-active: #b4befe;
        scrollbar-background: #181825;
        scrollbar-background-hover: #181825;
        scrollbar-background-active: #181825;
    }}

    AgentRow {{
        /* FIXED height (round border = +2 rows). Untagged card = 2 content
           lines -> 4. We avoid `height: auto` because it re-measures content
           width and wraps the right-aligned stat line, inflating the card.
           Overflow is hidden so a padded line can never spill/wrap visibly. */
        height: 4;
        padding: 0 1;
        margin: 0 0 1 0;
        border: round {DIM} 40%;
        overflow: hidden;
    }}
    AgentRow.tagged {{
        /* tagged card = 3 content lines (title + subtitle + snippet) -> 5. */
        height: 5;
    }}
    AgentRow.selected {{
        background: {SELBG};
        border: round {ACCENT};
        border-left: thick {ACCENT};
    }}
    AgentRow.selected-attn {{
        background: {SELBG};
        border: round {ATTN};
        border-left: thick {ATTN};
    }}

    .groupdivider {{
        height: 1;
        color: {DIM};
        margin: 1 0 1 1;
    }}

    #preview {{
        height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-color: #9399b2;
        scrollbar-color-hover: #b4befe;
        scrollbar-color-active: #b4befe;
        scrollbar-background: #181825;
        scrollbar-background-hover: #181825;
        scrollbar-background-active: #181825;
    }}

    /* DESTRUCTIVE kill-confirm modal: dim backdrop, centered small card with a
       red (ATTN) border to read clearly as a destructive action. */
    KillConfirmScreen {{
        align: center middle;
        background: $background 60%;
    }}
    #killdialog {{
        width: 60;
        height: auto;
        max-width: 80%;
        padding: 1 2;
        background: {BG};
        border: round {ATTN};
        border-title-color: {ATTN};
    }}
    #killbody {{ width: 1fr; height: auto; }}

    /* Inline REPLY overlay: same dim backdrop as the kill modal but an ACCENT
       (blue) border — this is a normal, non-destructive action. */
    ReplyScreen {{
        align: center middle;
        background: $background 60%;
    }}
    #replydialog {{
        width: 80;
        height: auto;
        max-width: 90%;
        padding: 1 2;
        background: {BG};
        border: round {ACCENT};
    }}
    #replyheader {{ width: 1fr; height: 1; }}
    #replyinput {{
        width: 1fr;
        height: 8;
        margin: 1 0;
        background: {BG};
        border: round {DIM} 50%;
        /* Match the two-pane (list/preview/context) thin muted scrollbar so the
           reply box doesn't render the blue-on-black Textual default. Same
           values as #agentlist / #preview / #context above. */
        scrollbar-size-vertical: 1;
        scrollbar-color: #9399b2;
        scrollbar-color-hover: #b4befe;
        scrollbar-color-active: #b4befe;
        scrollbar-background: #181825;
        scrollbar-background-hover: #181825;
        scrollbar-background-active: #181825;
    }}
    #replyhint {{ width: 1fr; height: 1; }}

    /* Generic CONFIRM modal: dim backdrop, centered card with an ACCENT (blue)
       border — this is a NON-destructive action (clearing a monitor error). */
    ConfirmScreen {{
        align: center middle;
        background: $background 60%;
    }}
    #confirmdialog {{
        width: 64;
        height: auto;
        max-width: 80%;
        padding: 1 2;
        background: {BG};
        border: round {ACCENT};
        border-title-color: {ACCENT};
    }}
    #confirmbody {{ width: 1fr; height: auto; }}

    Footer {{
        height: 1;
        background: {BG};
        color: {DIM};
        padding: 0 1;
    }}

    NameInputScreen, DirPickerScreen, ModelPickerScreen {{
        align: center middle;
        background: $background 60%;
    }}
    #namedialog, #dirdialog, #modeldialog {{
        width: 78;
        height: auto;
        max-width: 90%;
        padding: 1 2;
        background: {BG};
        border: round {ACCENT};
    }}
    #dirresults {{
        height: 14;
        width: 1fr;
    }}
    #modelresults {{
        height: auto;
        width: 1fr;
    }}
    """

    BINDINGS = [
        ("ctrl+c", "quit", "quit"),
        # escape clears the filter; declared as a priority binding so it is
        # handled before any focused-widget default (which would swallow it).
        Binding("escape", "clear_filter", "clear", priority=True),
        # Tab cycles between the Agents and Context tabs. A NON-PRINTABLE key on
        # purpose: the Agents tab consumes plain letters into the type-to-filter,
        # so a letter binding would be unreachable there. priority=True so it
        # wins over Textual's default Tab focus-traversal AND fires regardless of
        # which inner widget holds focus. Losing default Tab focus-traversal is
        # fine here — the cockpit navigates via arrow keys + type-to-filter, not
        # Tab focus. The tabs are also clickable (TabbedContent's built-in bar).
        Binding("tab", "cycle_tab", "tabs", priority=True),
    ]

    agents: reactive[list] = reactive(list)
    filter_text: reactive[str] = reactive("")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._selected_key: str | None = None  # session_id or session name
        self._filtered: list[Agent] = []
        # True while the filter Input is focused (filter mode, entered with `/`).
        # In command mode (default) plain letters are commands, NOT filter chars.
        self._filter_mode: bool = False
        # key -> stable AgentRow widget (Issue 1: in-place keyed updates so
        # refresh ticks never tear down + remount unchanged rows).
        self._rows: dict[str, AgentRow] = {}
        # one header Static per non-empty section (needs you / running /
        # inactive), keyed by section-state. Managed in place AFTER the AgentRow
        # sync so they never disturb the keyed reorder (which filters to
        # AgentRow instances).
        self._headers: dict[str, Static] = {}   # section-state -> header Static
        # Context-tab selection (SEPARATE from the Agents-tab machinery, which is
        # typed to Agent). Keyed on the ContextRow key (sid -> session8 -> pane).
        self._ctx_selected_key: str | None = None
        # Last gathered ContextRows, stashed by _refresh_context so the Context
        # tab's movement/Enter logic reads them without re-gathering.
        self._ctx_rows: list = []

    # ---- compose ----

    def compose(self) -> ComposeResult:
        yield Header(id="topbar")
        # TWO tabs: "Agents" (the existing two-pane view, behavior unchanged) and
        # "Context" (the ctx-monitor mirror). Tabs are clickable; Tab cycles
        # between them (a non-printable key so it never collides with the
        # type-to-filter feature on the Agents tab — plain letters are consumed
        # by the filter, so a letter binding would be unreachable there).
        with TabbedContent(id="tabs"):
            with TabPane("Agents", id="agents-tab"):
                with Horizontal(id="body"):
                    with Vertical(id="left"):
                        yield Static("agents", classes="panelabel")
                        with Horizontal(id="filterrow"):
                            yield Static("❯ / to filter…", id="filter")
                            yield Input(placeholder="filter…", id="filterinput")
                            yield Static("0/0", id="matchcount")
                        yield VerticalScroll(id="agentlist")
                    with Vertical(id="right"):
                        yield Static("preview", classes="panelabel")
                        yield PreviewPane(id="preview")
            with TabPane("Context", id="context-tab"):
                yield ContextPane(id="context")
        yield FooterBar()

    def on_mount(self) -> None:
        self.refresh_data()
        self.set_interval(REFRESH_SECONDS, self.refresh_data)
        # Fast ticker for the animated braille "working" glyph (see below).
        self.set_interval(0.1, self._tick_spinner)

    def _tick_spinner(self) -> None:
        """Advance the braille spinner ~10fps and re-render ONLY working rows in
        place (no remount), so the working glyph animates between the slower 1.5s
        data refreshes. Needs-input / idle rows are untouched. `self._rows` maps
        key -> the stable AgentRow widget."""
        AgentRow.spin_frame += 1
        for row in self._rows.values():
            if row.agent is not None and row.agent.state == "working":
                row.refresh()

    # ---- data refresh (threaded) ----

    @work(thread=True, exclusive=True)
    def refresh_data(self) -> None:
        agents = data.gather_agents()
        self.call_from_thread(self._apply_agents, agents)

    def _apply_agents(self, agents: list[Agent]) -> None:
        self.agents = agents
        # header counts
        total = len(agents)
        working = sum(1 for a in agents if a.state == "working")
        attention = sum(1 for a in agents if a.state == "needs-input")
        self.query_one(Header).update_counts(total, working, attention)
        self._rebuild_list()
        self._refresh_context(agents)

    def _refresh_context(self, agents: list[Agent]) -> None:
        """Update the Context tab from the SAME gathered agent list (no second
        tmux/ps sweep) on every refresh tick. All file reads (monitor-state.json,
        monitor.lock) live in data.py; this only hands prepared rows to the
        widget."""
        try:
            pane = self.query_one("#context", ContextPane)
        except Exception:
            return  # not mounted yet
        rows = data.gather_context_rows(agents)
        liveness = data.monitor_liveness()
        pane.update_rows(rows, liveness)
        # Stash rows so movement/Enter logic reads them without re-gathering.
        self._ctx_rows = rows
        # Default / repair selection: keep it on the same row across refreshes;
        # otherwise prefer the FIRST error row, else the first row.
        keys = [self._ctx_key(r) for r in rows]
        if self._ctx_selected_key not in keys:
            err_keys = [self._ctx_key(r) for r in rows if r.is_error]
            if err_keys:
                self._ctx_selected_key = err_keys[0]
            elif keys:
                self._ctx_selected_key = keys[0]
            else:
                self._ctx_selected_key = None
        self._refresh_ctx_selection_classes()

    @staticmethod
    def _ctx_key(r) -> str:
        """Stable key for a ContextRow: sid -> session8 -> pane. MUST match
        ContextPane._row_key so the app's selection lines up with the widgets."""
        return r.sid or r.session8 or r.pane or "-"

    def _refresh_ctx_selection_classes(self) -> None:
        """Toggle the .selected / .selected-attn class on the Context row widgets
        (mirrors _refresh_selection_classes for the Agents tab)."""
        try:
            pane = self.query_one("#context", ContextPane)
        except Exception:
            return
        for key, w in pane._row_widgets.items():
            if w.row is None:
                continue
            sel = key == self._ctx_selected_key
            w.remove_class("selected")
            w.remove_class("selected-attn")
            if sel:
                w.add_class("selected-attn" if w.row.is_error else "selected")

    def _ctx_move(self, delta: int) -> None:
        """Move the Context-tab selection by delta (wraps); mirror _move."""
        if not self._ctx_rows:
            return
        keys = [self._ctx_key(r) for r in self._ctx_rows]
        try:
            i = keys.index(self._ctx_selected_key)
        except ValueError:
            i = 0
        i = (i + delta) % len(keys)
        self._ctx_selected_key = keys[i]
        self._refresh_ctx_selection_classes()
        self._scroll_to_ctx_selected()

    def _scroll_to_ctx_selected(self) -> None:
        """Bring the selected Context row into view if off-screen (best-effort)."""
        key = self._ctx_selected_key
        if key is None:
            return
        try:
            pane = self.query_one("#context", ContextPane)
            w = pane._row_widgets.get(key)
            if w is not None:
                pane.scroll_to_widget(w, animate=False)
        except Exception:
            pass

    def _ctx_enter(self) -> None:
        """Enter on the selected Context row: if it's in ERROR, confirm + clear
        the monitor error (re-arm the watcher) via a per-sid reset flag the
        daemon picks up. NON-destructive — does NOT touch the live session."""
        row = None
        for r in self._ctx_rows:
            if self._ctx_key(r) == self._ctx_selected_key:
                row = r
                break
        if row is None:
            return
        if not row.is_error:
            self.notify("session is not in error", timeout=2)
            return
        sel_key = self._ctx_selected_key
        title = "Clear monitor error"
        body = (f"Re-arm the context watcher for {row.name}? Clears the "
                "monitor's error state — does NOT touch the running session.  "
                "(y / n)")

        def _on_decision(confirmed: bool | None) -> None:
            if confirmed is not True:
                return
            sid = row.sid
            if not sid:
                self.notify("no session id for this row", severity="error")
                return
            ok, msg = data.request_state_reset(sid)
            self.notify(msg, severity=("information" if ok else "error"),
                        timeout=3)
            if ok:
                # Optimistic instant feedback; the next refresh re-reads
                # monitor-state.json as ground truth.
                try:
                    pane = self.query_one("#context", ContextPane)
                    pane.set_row_state(sel_key, "watching", False)
                except Exception:
                    pass
                row.state_label = "watching"
                row.is_error = False
                self._refresh_ctx_selection_classes()

        self.push_screen(ConfirmScreen(title, body), _on_decision)

    # ---- filtering ----

    def _matches(self, agents: list[Agent]) -> list[Agent]:
        q = self.filter_text.strip().lower()
        if not q:
            return agents
        out = []
        for a in agents:
            # haystack = the label PLUS the session name Kiran actually sees on
            # the card (the cleaned pane_title), so he can filter by what's on
            # screen. Joined with spaces so token-fuzzy still works.
            extras = a.pane_title or ""
            hay = (a.label + " " + extras).lower().strip()
            if q in hay:
                out.append(a)
            elif difflib.SequenceMatcher(None, q, hay).ratio() > 0.55:
                out.append(a)
            else:
                # token-level fuzzy: any word close-match
                if any(difflib.SequenceMatcher(None, q, w).ratio() > 0.7
                       for w in hay.replace("·", " ").split()):
                    out.append(a)
        return out

    # ---- list rendering ----

    def _rebuild_list(self) -> None:
        agents = list(self.agents)
        self._filtered = self._matches(agents)

        # keep selection on the same agent across refresh/filter
        keys = [self._key(a) for a in self._filtered]
        if self._selected_key not in keys:
            self._selected_key = keys[0] if keys else None

        self._sync_rows(keys)
        self._sync_sections()
        self._refresh_selection_classes()

        self.query_one("#matchcount", Static).update(
            f"{len(self._filtered)}/{len(agents)}")
        self._update_preview()

    def _sync_sections(self) -> None:
        """Place a header above each non-empty section (needs you / running /
        inactive). Managed AFTER the AgentRow sync so it never interferes with
        the keyed no-remount reorder (which filters to AgentRow instances).

        `self._filtered` is already section-sorted by gather_agents, so each
        section's first index is just the first row whose state maps to it."""
        from rich.text import Text
        listview = self.query_one("#agentlist", VerticalScroll)
        first_idx: dict[str, int] = {}
        counts: dict[str, int] = {}
        for i, a in enumerate(self._filtered):
            sec = a.state if a.state in SECTION_LABELS else "idle"
            counts[sec] = counts.get(sec, 0) + 1
            first_idx.setdefault(sec, i)
        # drop headers for sections that are now empty
        for sec in list(self._headers):
            if sec not in first_idx:
                try:
                    self._headers.pop(sec).remove()
                except Exception:
                    self._headers.pop(sec, None)
        # ensure + label + position a header for each non-empty section, in order
        styles = {"needs-input": f"bold {ATTN}", "working": f"bold {ACCENT}", "idle": DIM}
        for sec in SECTION_ORDER:
            if sec not in first_idx:
                continue
            hdr = self._headers.get(sec)
            if hdr is None:
                hdr = Static(classes="groupdivider")
                self._headers[sec] = hdr
                listview.mount(hdr)
            hdr.update(Text(f"{SECTION_LABELS[sec]} · {counts[sec]}", style=styles[sec]))
            first_row = self._rows.get(self._key(self._filtered[first_idx[sec]]))
            try:
                if first_row is not None:
                    listview.move_child(hdr, before=first_row)
            except Exception:
                pass

    def _sync_rows(self, desired_keys: list[str]) -> None:
        """Diff the desired ordered key list against the mounted AgentRows.

        IN-PLACE update strategy (Issue 1 — the flicker fix):
          - key still present  -> reassign `row.agent` (reactive -> re-render);
            the SAME widget object stays mounted (no remount).
          - key appeared       -> mount() a new AgentRow.
          - key vanished       -> remove() its row and drop it from `self._rows`.
          - order changed      -> move_child() the existing rows into the new
            order (move != remount; widget identity is preserved).

        Steady state (same keys, same order) does ZERO mount/remove/move — the
        only work is the in-place `row.agent = ...` content refresh.
        """
        listview = self.query_one("#agentlist", VerticalScroll)
        by_key = {self._key(a): a for a in self._filtered}
        desired_set = set(desired_keys)

        # 1. remove rows whose key vanished
        for key in list(self._rows):
            if key not in desired_set:
                self._rows.pop(key).remove()

        # 2. update existing rows in place; mount new ones at the end (order is
        #    fixed up in step 3). Reassigning the reactive re-renders without a
        #    remount, preserving widget identity.
        for key in desired_keys:
            agent = by_key[key]
            row = self._rows.get(key)
            if row is None:
                row = AgentRow(agent, False)
                self._rows[key] = row
                listview.mount(row)
            else:
                row.agent = agent  # reactive(layout=True) -> in-place re-render

        # 3. reorder mounted rows to match desired order WITHOUT remounting.
        #    move_child preserves widget identity (it relocates, not recreates).
        #    SCROLL STABILITY (Issue 1 / Change 1): capture the list's scroll
        #    offset and restore it after reordering so a refresh never yanks the
        #    scrollbar. A steady-state tick (unchanged order) skips the reorder
        #    entirely AND leaves scroll untouched -> zero scroll jump.
        try:
            saved_offset = listview.scroll_offset
            current = [c for c in listview.children if isinstance(c, AgentRow)]
            current_keys = [self._key(c.agent) for c in current
                            if c.agent is not None]
            if current_keys != desired_keys:
                prev: AgentRow | None = None
                for key in desired_keys:
                    row = self._rows[key]
                    if prev is None:
                        listview.move_child(row, before=0)
                    else:
                        listview.move_child(row, after=prev)
                    prev = row
                # restore the pre-reorder scroll position so the visible window
                # doesn't jump; the selected row is re-revealed by the caller
                # (_rebuild_list -> only if off-screen) when appropriate.
                try:
                    listview.scroll_to(
                        x=saved_offset.x, y=saved_offset.y, animate=False)
                except Exception:
                    pass
        except Exception:
            # If move_child is unavailable/misbehaves on this build, the rows are
            # still all present and updated in place — only ordering may lag a
            # tick. We deliberately do NOT fall back to remove+remount (that's
            # the flicker we are fixing).
            pass

    def _update_preview(self) -> None:
        sel = self.selected_agent
        self.query_one(PreviewPane).show(sel)

    @staticmethod
    def _key(a: Agent) -> str:
        return a.session_id or a.session

    @property
    def selected_agent(self) -> Agent | None:
        for a in self._filtered:
            if self._key(a) == self._selected_key:
                return a
        return None

    def _modal_active(self) -> bool:
        """True while a ModalScreen (reply or kill-confirm) is on the stack.

        When true the background list must be fully inert: every command
        action_* and the global on_key early-return so ONLY the modal responds
        to input. The modal owns its own keys (typing, Enter/Shift+Enter, y/n).
        `escape` is the one exception NOT blanket-guarded: it reaches the App's
        priority `escape` binding (action_clear_filter), which dismisses the
        active modal — so Esc still cancels both modals."""
        return isinstance(self.screen, (ReplyScreen, KillConfirmScreen, ConfirmScreen,
                                        NameInputScreen, DirPickerScreen, ModelPickerScreen))

    # ---- selection movement ----

    def _move(self, delta: int) -> None:
        if not self._filtered:
            return
        keys = [self._key(a) for a in self._filtered]
        try:
            i = keys.index(self._selected_key)
        except ValueError:
            i = 0
        i = (i + delta) % len(keys)
        self._selected_key = keys[i]
        self._refresh_selection_classes()
        self._update_preview()
        self._scroll_to_selected()

    def _refresh_selection_classes(self) -> None:
        for row in self.query(AgentRow):
            if row.agent is None:
                continue
            sel = self._key(row.agent) == self._selected_key
            row.remove_class("selected")
            row.remove_class("selected-attn")
            if sel:
                row.add_class("selected-attn"
                              if row.agent.state == "needs-input"
                              else "selected")

    def _scroll_to_selected(self) -> None:
        """Bring the selected row into view if it's off-screen.

        ROOT CAUSE of the prior breakage (Change C): a selection move reassigns
        the selected row's reactive (and may move_child it), scheduling a
        re-layout. Calling scroll_visible() SYNCHRONOUSLY here ran against the
        STALE pre-layout geometry, so it only nudged the offset ~1 line per
        press instead of bringing the (5-row) card fully into the viewport —
        the card marched off the bottom and never recovered.

        FIX: DEFER the scroll to AFTER the refresh settles via
        call_after_refresh, and RE-QUERY the persistent keyed row + the
        #agentlist container at call time (never capture a stale widget). We
        scroll the #agentlist VerticalScroll DIRECTLY with scroll_to_widget so
        it can't bubble to the wrong ancestor. animate=False keeps the scrollbar
        from glitching across rapid ↑/↓ presses; scroll_to_widget is a no-op
        when the row is already fully visible, so an on-screen move never jumps.
        """
        if self._selected_key is None:
            return
        self.call_after_refresh(self._do_scroll_to_selected)

    def _do_scroll_to_selected(self) -> None:
        """Deferred scroll body — runs after the post-selection re-layout has
        settled, re-reading the live row + container so the geometry is fresh.
        """
        key = self._selected_key
        if key is None:
            return
        row = self._rows.get(key)
        if row is None or row.agent is None:
            return
        try:
            lv = self.query_one("#agentlist", VerticalScroll)
            lv.scroll_to_widget(row, animate=False)
        except Exception:
            pass

    # ---- key actions ----

    def action_move_down(self) -> None:
        if self._modal_active():
            return
        self._move(1)

    def action_move_up(self) -> None:
        if self._modal_active():
            return
        self._move(-1)

    def action_next_alert(self) -> None:
        """→ : jump selection to the next needs-input agent (wrap)."""
        if self._modal_active():
            return
        needy = [a for a in self._filtered if a.state == "needs-input"]
        if not needy:
            self.notify("no agents need attention", timeout=2)
            return
        nkeys = [self._key(a) for a in needy]
        # find current position among ALL filtered, then next needy after it
        akeys = [self._key(a) for a in self._filtered]
        try:
            cur = akeys.index(self._selected_key)
        except ValueError:
            cur = -1
        # walk forward from cur+1, wrapping, to the first needy
        n = len(akeys)
        for step in range(1, n + 1):
            cand = akeys[(cur + step) % n]
            if cand in nkeys:
                self._selected_key = cand
                break
        self._refresh_selection_classes()
        self._update_preview()
        self._scroll_to_selected()

    def action_open_window(self) -> None:
        """⏎ : focus the selected agent's Ghostty window via aerospace.

        wid resolution (data.resolve_wid): stamped @aerospace_wid first, else a
        best-effort pane_title -> aerospace-window-title match. On an AMBIGUOUS
        title match (2+ windows) we REFUSE to focus a possibly-wrong window and
        say so; on no match we keep the existing "attach it once" message.
        """
        if self._modal_active():
            return
        a = self.selected_agent
        if a is None:
            return
        wid = data.resolve_wid(a)
        if wid == data.AMBIGUOUS_WID:
            name = a.pane_title or a.label
            self.notify(
                f"ambiguous window mapping for '{name}' — attach it once",
                timeout=3)
            return
        if not wid:
            self.notify("no window mapping yet — attach it once", timeout=3)
            return
        ok = data.focus_window(wid)
        if not ok:
            self.notify(f"could not focus window {wid} (gone?)", timeout=3)

    def action_clear_filter(self) -> None:
        # The App declares `escape` as a PRIORITY binding. Textual resolves
        # priority bindings App-FIRST (App._check_bindings iterates
        # `reversed(screen._binding_chain)`), so this fires even when a modal is
        # open — making the App the single owner of escape -> modal dismissal
        # (a modal-level escape binding would be shadowed by this one). The
        # blanket on_key modal-guard deliberately does NOT cover escape, so this
        # still runs while a modal is up.
        # 1. kill-confirm modal open -> escape CANCELS the modal.
        if isinstance(self.screen, KillConfirmScreen):
            self.screen.dismiss(False)
            return
        # 2. reply modal open -> escape CANCELS the modal (without sending).
        if isinstance(self.screen, ReplyScreen):
            self.screen.dismiss(False)
            return
        # 2b. generic confirm modal open -> escape CANCELS it.
        if isinstance(self.screen, ConfirmScreen):
            self.screen.dismiss(False)
            return
        # 2c. new-agent flow modals -> escape CANCELS them.
        if isinstance(self.screen, (NameInputScreen, DirPickerScreen, ModelPickerScreen)):
            self.screen.dismiss(False)
            return
        # 3. filter mode -> escape CANCELS the filter (clears + back to command).
        if self._filter_mode:
            self._exit_filter_mode(keep=False)
            return
        # 4. otherwise idempotent: reset to the full list.
        self.filter_text = ""
        self._update_filter_display()
        self._rebuild_list()

    # ---- tab switching ----

    def _active_tab(self) -> str:
        """Id of the currently-active TabPane ('agents-tab' / 'context-tab').

        Defaults to 'agents-tab' before the TabbedContent has mounted (so early
        key events behave as the Agents tab)."""
        try:
            return self.query_one("#tabs", TabbedContent).active or "agents-tab"
        except Exception:
            return "agents-tab"

    def action_cycle_tab(self) -> None:
        """Tab: cycle to the NEXT tab, wrapping (so it still works if a 3rd tab
        is ever added).

        GUARD: when the DESTRUCTIVE kill-confirm modal is open, Tab must NOT
        switch the underlying tabs. App priority bindings are checked before the
        focused ModalScreen's handlers (same reason `escape` defers to the modal
        in action_clear_filter), so we no-op here while that modal is on the
        stack and let the keystroke stay within the modal.
        """
        if self._modal_active():
            return
        # In filter mode the Input owns Tab (e.g. focus); don't switch tabs.
        if self._filter_mode:
            return
        try:
            tabs = self.query_one("#tabs", TabbedContent)
        except Exception:
            return
        # Cycle through the TabPane ids in declared order, wrapping.
        pane_ids = [p.id for p in tabs.query(TabPane) if p.id]
        if not pane_ids:
            return
        try:
            i = pane_ids.index(tabs.active)
        except ValueError:
            i = 0
        tabs.active = pane_ids[(i + 1) % len(pane_ids)]

    # ---- kill-session flow (DESTRUCTIVE — confirm modal is non-negotiable) ----

    def _request_kill_selected(self) -> None:
        """Begin the kill flow for the selected agent (Backspace, empty filter).

        SAFETY:
          - No selection -> no-op.
          - SELF-GUARD: if the selected agent's tmux session == the session the
            cockpit ITSELF is attached to, REFUSE outright (no modal, just a
            toast). We never kill the cockpit's own session.
          - Otherwise push the mandatory KillConfirmScreen; the kill only runs
            if the user explicitly confirms (`y`). Default is cancel.
        """
        if self._modal_active():
            return
        a = self.selected_agent
        if a is None:
            return
        self_sess = data.current_tmux_session()
        if self_sess is not None and a.session == self_sess:
            self.notify("can't kill the cockpit's own session", timeout=3)
            return
        display_name = AgentRow._card_title(a)
        tmux_session = a.session

        def _on_decision(confirmed: bool | None) -> None:
            # Only an explicit True (user pressed `y`) proceeds. None (dismissed
            # without a value) and False both cancel.
            if confirmed is True:
                self._do_kill(tmux_session)

        self.push_screen(
            KillConfirmScreen(display_name, tmux_session), _on_decision)

    def _do_kill(self, tmux_session: str) -> None:
        """Run the confirmed kill and tidy up selection + the list.

        On success: move selection to a sensible NEIGHBOR (next filtered agent,
        or previous if the killed one was last) so selection never points at a
        dead key, then refresh the list so the row disappears promptly. On
        failure: a brief toast.
        """
        # Compute the neighbor BEFORE the kill (while the row is still present),
        # so we know where to land selection.
        neighbor_key = self._neighbor_key(tmux_session)

        ok = data.kill_session(tmux_session)
        if not ok:
            self.notify(f"failed to kill '{tmux_session}'", timeout=3)
            return

        # land selection on the neighbor (may be None if it was the only row)
        self._selected_key = neighbor_key
        # promptly drop the killed row + re-render rather than wait the full
        # ~1.5s refresh tick. _apply_agents reuses the last gathered list with
        # the dead session filtered out for an instant visual update; the next
        # scheduled refresh_data() then re-confirms against live tmux.
        survivors = [a for a in self.agents if a.session != tmux_session]
        self._apply_agents(survivors)

    def _neighbor_key(self, dead_session: str) -> str | None:
        """Key to select after killing the agent whose session == dead_session.

        Next agent in the current filtered list, or the previous one if the
        dead agent was last. None when nothing else remains.
        """
        keys = [self._key(a) for a in self._filtered]
        # find the dead agent's index by session (its key may be session_id)
        dead_idx = None
        for i, a in enumerate(self._filtered):
            if a.session == dead_session:
                dead_idx = i
                break
        if dead_idx is None:
            return self._selected_key  # not in list; leave as-is
        survivors = [k for j, k in enumerate(keys) if j != dead_idx]
        if not survivors:
            return None
        # prefer the next row; fall back to the previous (dead was last).
        if dead_idx < len(survivors):
            return survivors[dead_idx]
        return survivors[-1]

    # ---- filter mode (entered with `/`) ----

    def _enter_filter_mode(self) -> None:
        """Show + focus the filter Input. The Input's value (not captured key
        events) now drives the live filter via on_input_changed."""
        self._filter_mode = True
        inp = self.query_one("#filterinput", Input)
        self.query_one("#filter", Static).add_class("filtering")
        inp.add_class("filtering")
        # seed the Input with any pre-existing filter so re-entering edits it.
        inp.value = self.filter_text
        inp.focus()

    def _exit_filter_mode(self, *, keep: bool) -> None:
        """Leave filter mode -> command mode.

        keep=True  -> LOCK: the filter stays applied; hide the Input and focus
                      the (filtered) list with the top result selected so the
                      user can immediately act on it.
        keep=False -> CANCEL: clear the filter and return to the full list.
        """
        self._filter_mode = False
        inp = self.query_one("#filterinput", Input)
        inp.remove_class("filtering")
        self.query_one("#filter", Static).remove_class("filtering")
        if not keep:
            self.filter_text = ""
            inp.value = ""
            self._rebuild_list()
        else:
            # land selection on the top filtered result so it's immediately
            # actionable (reply/open/kill) from command mode.
            if self._filtered:
                self._selected_key = self._key(self._filtered[0])
                self._refresh_selection_classes()
                self._update_preview()
                self._scroll_to_selected()
        self._update_filter_display()
        # return focus to the app (away from the Input) so command keys work.
        try:
            self.query_one("#agentlist", VerticalScroll).focus()
        except Exception:
            self.set_focus(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live-filter as the user types in the filter Input."""
        if event.input.id != "filterinput":
            return
        self.filter_text = event.value
        self._rebuild_list()

    # ---- reply (entered with `r`) ----

    def _request_reply_selected(self) -> None:
        """Open the inline Reply overlay for the selected agent.

        No-op (with a toast) when nothing is selected. Reply is driven from the
        AGENTS tab's selected card; on the Context tab `r` does nothing (v1).
        """
        if self._modal_active():
            return
        a = self.selected_agent
        if a is None:
            self.notify("no agent selected", timeout=2)
            return
        pane = a.active_pane
        if not pane:
            self.notify("no pane to send to", timeout=2)
            return
        display_name = AgentRow._card_title(a)
        self.push_screen(ReplyScreen(display_name, pane))

    def _request_new_agent(self) -> None:
        """N: launch the 3-step new-agent flow (name -> dir -> model -> spawn)."""
        if self._modal_active():
            return

        def _after_name(name):
            if not name:
                return
            dirs = data.recent_claude_dirs()

            def _after_dir(directory):
                if not directory:
                    return

                def _after_model(model_id):
                    if not model_id:
                        return
                    color = random.choice(SPAWN_COLORS)
                    ok, msg = data.spawn_claude_window(name, directory, model_id, color)
                    self.notify(msg, severity=("information" if ok else "error"),
                                timeout=4)

                self.push_screen(ModelPickerScreen(), _after_model)

            self.push_screen(DirPickerScreen(dirs), _after_dir)

        self.push_screen(NameInputScreen(), _after_name)

    # ---- raw key handling (modal command/filter input model) ----

    # Non-printable nav/command keys handled here; everything else falls through.
    _NAV_KEYS = {"down", "up", "right", "enter", "escape", "backspace",
                 "left", "tab", "shift+tab", "home", "end", "pageup",
                 "pagedown"}

    def on_key(self, event) -> None:
        key = event.key
        # MODAL GUARD (the main capture fix): while ANY ModalScreen (reply or
        # kill-confirm) is on the stack, the background list must be completely
        # inert. Return WITHOUT stopping the event so it stays with the modal
        # screen (which owns typing, Enter/Shift+Enter, and its own priority
        # `escape` cancel). Every command action_* also guards on _modal_active,
        # but this short-circuit keeps the App's global on_key from acting on
        # backspace (kill) / arrows (move) / r / slash etc. while a modal is up.
        if self._modal_active():
            return
        # While in filter mode the Input owns the keyboard: Enter LOCKS the
        # filter, Esc CANCELS it; all other keys flow to the Input (live filter
        # via on_input_changed). We intercept ONLY enter/escape here.
        if self._filter_mode:
            if key == "enter":
                self._exit_filter_mode(keep=True); event.stop(); return
            if key == "escape":
                self._exit_filter_mode(keep=False); event.stop(); return
            return  # let the Input handle the keystroke
        # On the CONTEXT tab we handle arrow selection + Enter (clear monitor
        # error), and let everything else fall through so the pane still scrolls
        # and the priority Bindings (Tab/ctrl+c/escape) fire independently.
        if self._active_tab() != "agents-tab":
            if key == "down":
                self._ctx_move(1); event.stop(); return
            if key == "up":
                self._ctx_move(-1); event.stop(); return
            if key == "enter":
                self._ctx_enter(); event.stop(); return
            return
        # COMMAND MODE. Plain letters are NO LONGER a filter.
        if key == "down":
            self.action_move_down(); event.stop(); return
        if key == "up":
            self.action_move_up(); event.stop(); return
        if key == "right":
            self.action_next_alert(); event.stop(); return
        if key == "enter":
            self.action_open_window(); event.stop(); return
        if key == "slash":
            self._enter_filter_mode(); event.stop(); return
        if key == "r":
            self._request_reply_selected(); event.stop(); return
        if key in ("n", "N"):
            self._request_new_agent(); event.stop(); return
        if key == "escape":
            # handled by the priority Binding; keep here as a fallback.
            self.action_clear_filter(); event.stop(); return
        if key == "q":
            self.action_quit(); event.stop(); return
        if key == "backspace":
            # Backspace triggers the kill-confirm flow on the selected agent.
            self._request_kill_selected()
            event.stop(); return
        # All other keys (including plain letters) are ignored in command mode.

    def _update_filter_display(self) -> None:
        w = self.query_one("#filter", Static)
        if self.filter_text:
            t = Text()
            t.append("❯ ", style=ACCENT)
            t.append(self.filter_text, style=BRIGHT)
            w.update(t)
        else:
            w.update(Text("❯ / to filter…", style=DIM))

    # mouse click selects a row (nice-to-have)
    def on_click(self, event) -> None:
        # find an AgentRow ancestor of the clicked widget
        widget = event.widget
        while widget is not None:
            if isinstance(widget, AgentRow):
                self._selected_key = self._key(widget.agent)
                self._refresh_selection_classes()
                self._update_preview()
                self._scroll_to_selected()
                return
            widget = widget.parent


class FooterBar(Static):
    """Footer key-hint bar with inverse key chips + dim labels."""

    def on_mount(self) -> None:
        self.update(self._hints())

    def _hints(self) -> Text:
        t = Text()
        chips = [
            ("↿⇂", "move"),
            ("r", "reply"),
            ("/", "filter"),
            ("⏎", "open"),
            ("→", "next alert"),
            ("Tab", "tab"),
            ("⌫", "kill"),
            ("^c", "quit"),
        ]
        first = True
        for key, label in chips:
            if not first:
                t.append("  ·  ", style=DIM)
            first = False
            if key:
                t.append(f" {key} ", style=f"reverse {BRIGHT}")
                t.append(" ", style=DIM)
            t.append(label, style=DIM)
        return t


def main() -> None:
    AgentsApp().run()


if __name__ == "__main__":
    main()
