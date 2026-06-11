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
from datetime import datetime

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

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
    """Top bar: title left, live counts + clock right."""

    def update_counts(self, total: int, working: int, attention: int) -> None:
        t = Text()
        t.append("◆ ", style=f"bold {ACCENT}")
        t.append("agents", style=f"bold {BRIGHT}")
        t.append(" tui", style=DIM)

        right = Text()
        right.append(f"{total} agents", style=BRIGHT)
        right.append("  ·  ", style=DIM)
        right.append(f"{working} working", style=ACCENT)
        right.append("  ·  ", style=DIM)
        right.append(f"{attention} need attention",
                     style=ATTN if attention else DIM)
        right.append("  ·  ", style=DIM)
        right.append(datetime.now().strftime("%H:%M:%S"), style=DIM)

        # pad between left and right to fill the width
        width = max(self.size.width, 40)
        gap = width - t.cell_len - right.cell_len
        if gap < 1:
            gap = 1
        t.append(" " * gap)
        t.append_text(right)
        self.update(t)


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
        """Headline for the card. Precedence: @cc_name tag -> cleaned pane_title
        (the live Claude session name) -> `project · task` label fallback."""
        return a.tag_name or a.pane_title or a.label

    @staticmethod
    def _has_subtitle(a: Agent) -> bool:
        """True when the title came from tag_name or pane_title (i.e. it's NOT
        the `project · task` label), so the label shows as a dim subtitle and
        the card needs the extra (3rd) content line."""
        return bool(a.tag_name or a.pane_title)

    def _apply_tagged_class(self, agent: Agent | None) -> None:
        """Toggle the `.tagged` class so the CSS picks the right fixed height.

        A card with a SUBTITLE has 3 content lines (title + subtitle + snippet)
        and needs one extra row vs a plain 2-line card. The subtitle appears
        whenever the title came from a tag_name OR a cleaned pane_title (most
        live sessions have a pane_title, so most cards are now 3-line). We use a
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
        # @cc_name tag -> cleaned pane_title (the live Claude session name) ->
        # `project · task`. When the headline is NOT the label, `project · task`
        # drops to a dim subtitle line below.
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

        # SUBTITLE (when the title is a tag_name or pane_title): dim
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
    # any focused-widget default. `escape` is handled in on_key (NOT as a
    # binding) because the App declares a priority `escape` binding
    # (clear_filter) that would otherwise win even while the modal is open.
    # Enter is intentionally NOT bound to confirm — a stray Enter must not kill.
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


class AgentsApp(App):
    """The agents-tui application."""

    CSS = f"""
    Screen {{ background: {BG}; }}

    Header {{
        height: 1;
        padding: 0 1;
        background: {BG};
        color: {BRIGHT};
    }}

    #body {{ height: 1fr; }}

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

    Footer {{
        height: 1;
        background: {BG};
        color: {DIM};
        padding: 0 1;
    }}
    """

    BINDINGS = [
        ("ctrl+c", "quit", "quit"),
        # escape clears the filter; declared as a priority binding so it is
        # handled before any focused-widget default (which would swallow it).
        Binding("escape", "clear_filter", "clear", priority=True),
    ]

    agents: reactive[list] = reactive(list)
    filter_text: reactive[str] = reactive("")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._selected_key: str | None = None  # session_id or session name
        self._filtered: list[Agent] = []
        # key -> stable AgentRow widget (Issue 1: in-place keyed updates so
        # refresh ticks never tear down + remount unchanged rows).
        self._rows: dict[str, AgentRow] = {}
        # one header Static per non-empty section (needs you / running /
        # inactive), keyed by section-state. Managed in place AFTER the AgentRow
        # sync so they never disturb the keyed reorder (which filters to
        # AgentRow instances).
        self._headers: dict[str, Static] = {}   # section-state -> header Static

    # ---- compose ----

    def compose(self) -> ComposeResult:
        yield Header(id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("agents", classes="panelabel")
                with Horizontal(id="filterrow"):
                    yield Static("❯ type to filter…", id="filter")
                    yield Static("0/0", id="matchcount")
                yield VerticalScroll(id="agentlist")
            with Vertical(id="right"):
                yield Static("preview", classes="panelabel")
                yield PreviewPane(id="preview")
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

    # ---- filtering ----

    def _matches(self, agents: list[Agent]) -> list[Agent]:
        q = self.filter_text.strip().lower()
        if not q:
            return agents
        out = []
        for a in agents:
            # haystack = the label PLUS the session name Kiran actually sees on
            # the card (tag_name and/or cleaned pane_title), so he can filter by
            # what's on screen. Joined with spaces so token-fuzzy still works.
            extras = " ".join(x for x in (a.tag_name, a.pane_title) if x)
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
        self._move(1)

    def action_move_up(self) -> None:
        self._move(-1)

    def action_next_alert(self) -> None:
        """→ : jump selection to the next needs-input agent (wrap)."""
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
        # The App declares `escape` as a PRIORITY binding, so it fires even when
        # the kill-confirm modal is open (priority bindings are checked before
        # the focused screen's on_key). When the modal IS open, escape must
        # CANCEL the modal, not clear the filter — defer to it.
        if isinstance(self.screen, KillConfirmScreen):
            self.screen.dismiss(False)
            return
        # idempotent: always reset to the full list on esc.
        self.filter_text = ""
        self._update_filter_display()
        self._rebuild_list()

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

    # ---- raw key handling (filter typing + nav) ----

    # keys we explicitly handle as navigation/commands (never typed into filter)
    _NAV_KEYS = {"down", "up", "right", "enter", "escape", "backspace",
                 "left", "tab", "shift+tab", "home", "end", "pageup",
                 "pagedown"}

    def on_key(self, event) -> None:
        key = event.key
        if key == "down":
            self.action_move_down(); event.stop(); return
        if key == "up":
            self.action_move_up(); event.stop(); return
        if key == "right":
            self.action_next_alert(); event.stop(); return
        if key == "enter":
            self.action_open_window(); event.stop(); return
        if key == "escape":
            # handled by the priority Binding; keep here as a fallback.
            self.action_clear_filter(); event.stop(); return
        if key == "q" and not self.filter_text:
            # bare `q` quits; `q` typed into a non-empty filter is a char.
            self.action_quit(); event.stop(); return
        if key == "backspace":
            if self.filter_text:
                # non-empty filter: Backspace edits the filter (delete a char).
                self.filter_text = self.filter_text[:-1]
                self._update_filter_display()
                self._rebuild_list()
            else:
                # empty filter: Backspace triggers the kill-confirm flow on the
                # selected agent (mirrors the `q`-quits-only-when-empty rule).
                self._request_kill_selected()
            event.stop(); return
        if key in self._NAV_KEYS:
            return  # leave other nav keys to default handling
        # printable single chars (incl. space) -> filter
        ch = event.character
        if ch is not None and len(ch) == 1 and (ch == " " or ch.isprintable()):
            self.filter_text += ch
            self._update_filter_display()
            self._rebuild_list()
            event.stop()

    def _update_filter_display(self) -> None:
        w = self.query_one("#filter", Static)
        if self.filter_text:
            t = Text()
            t.append("❯ ", style=ACCENT)
            t.append(self.filter_text, style=BRIGHT)
            w.update(t)
        else:
            w.update(Text("❯ type to filter…", style=DIM))

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
            ("⏎", "open window"),
            ("→", "next alert"),
            ("", "type filter"),
            ("esc", "clear"),
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
