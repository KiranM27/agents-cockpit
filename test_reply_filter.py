"""test_reply_filter.py — proofs for the modal input model: REPLY + FILTER.

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_reply_filter.py

SAFETY: NO real tmux send EVER happens. data.send_message_to_pane is
monkeypatched to a stub that records its args; the headless app never touches
a live pane. Agents are INJECTED synthetically so the proofs don't depend on
the live fleet.

Proves (headless run_test, size=120x40):
  F1. `/` enters filter mode: the filter Input is shown (.filtering) + focused.
  F2. Typing into the Input live-filters the list (match count shrinks).
  F3. Enter LOCKS the filter: back to command mode, Input hidden, filter STILL
      applied, list focused with the top result selected.
  F4. Esc from filter mode CANCELS: clears the filter, full list restored.
  F5. Plain letters in COMMAND mode do NOT filter (pressing 'x' leaves the
      filter empty + the full list intact).
  R1. `r` opens the ReplyScreen modal for the selected agent.
  R2. Typing into the reply TextArea works; Shift+Enter inserts a newline
      (multi-line draft).
  R3. Enter SENDS: calls the (stubbed) send with the EXACT drafted text and the
      selected agent's pane, then dismisses the modal.
  R4. Esc in the reply modal CANCELS: dismisses WITHOUT calling send.
  R5. `r` with nothing selected is a no-op (no modal pushed).
  T1. Tab still switches tabs (agents-tab <-> context-tab).
  K1. The kill modal still opens on Backspace (regression guard).

No transcript CONTENT is printed.
"""

from __future__ import annotations

import asyncio
import sys

from agents_tui import data
from agents_tui.app import AgentsApp, KillConfirmScreen, ReplyScreen
from agents_tui.data import Agent
from textual.widgets import Input, TextArea


def make_agents() -> list[Agent]:
    """Three synthetic agents with distinct labels + pane ids for filtering."""
    return [
        Agent(session="cc-alpha-1", session_id="sid-alpha",
              active_pane="%11", project="alpha", task="build",
              state="idle", pane_title="alpha-build"),
        Agent(session="cc-beta-2", session_id="sid-beta",
              active_pane="%22", project="beta", task="review",
              state="idle", pane_title="beta-review"),
        Agent(session="cc-gamma-3", session_id="sid-gamma",
              active_pane="%33", project="gamma", task="deploy",
              state="idle", pane_title="gamma-deploy"),
    ]


async def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    # Stub the send function — records (pane, text); NEVER touches tmux.
    sent: list[tuple[str, str]] = []
    orig_send = data.send_message_to_pane

    def stub_send(pane: str, text: str):
        sent.append((pane, text))
        return (True, "")

    data.send_message_to_pane = stub_send

    print("===== REPLY + FILTER TESTS =====")
    try:
        app = AgentsApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Freeze the live refresh so injected synthetic agents persist
            # (otherwise the 1.5s timer overwrites them with the real fleet and
            # the proofs become non-deterministic).
            app.refresh_data = lambda: None
            # Inject synthetic agents so proofs don't depend on the live fleet.
            app._apply_agents(make_agents())
            await pilot.pause()
            # select the first agent deterministically
            app._selected_key = app._key(app._filtered[0])
            app._refresh_selection_classes()
            await pilot.pause()

            total = len(app._filtered)
            print(f"    injected agents           : {total}")

            # ---- F5: plain letters in command mode do NOT filter ----
            await pilot.press("x")
            await pilot.pause()
            no_filter = app.filter_text == ""
            full_list = len(app._filtered) == total
            not_filter_mode = app._filter_mode is False
            print(f"    F5 after 'x': filter_text={app.filter_text!r} "
                  f"filtered={len(app._filtered)} filter_mode={app._filter_mode}")
            check("F5.plain_letter_does_not_filter",
                  no_filter and full_list and not_filter_mode,
                  f"(filter={app.filter_text!r} n={len(app._filtered)})")

            # ---- F1: `/` enters filter mode (Input shown + focused) ----
            await pilot.press("slash")
            await pilot.pause()
            inp = app.query_one("#filterinput", Input)
            shown = inp.has_class("filtering")
            focused = app.focused is inp
            print(f"    F1 after '/': filter_mode={app._filter_mode} "
                  f"input_shown={shown} input_focused={focused}")
            check("F1.slash_enters_filter_mode",
                  app._filter_mode and shown and focused,
                  f"(mode={app._filter_mode} shown={shown} focused={focused})")

            # ---- F2: typing live-filters ----
            for ch in "beta":
                await pilot.press(ch)
            await pilot.pause()
            filtered_n = len(app._filtered)
            only_beta = all("beta" in (a.label + " " + (a.pane_title or "")).lower()
                            for a in app._filtered) and filtered_n >= 1
            shrank = filtered_n < total
            print(f"    F2 typed 'beta': input={inp.value!r} "
                  f"filter_text={app.filter_text!r} filtered={filtered_n}")
            check("F2.typing_live_filters",
                  shrank and only_beta and app.filter_text == "beta",
                  f"(n={filtered_n} filter={app.filter_text!r})")

            # ---- F3: Enter LOCKS the filter ----
            await pilot.press("enter")
            await pilot.pause()
            locked_applied = app.filter_text == "beta" and len(app._filtered) == filtered_n
            cmd_mode = app._filter_mode is False
            input_hidden = not inp.has_class("filtering")
            top_selected = (app._filtered
                            and app._selected_key == app._key(app._filtered[0]))
            print(f"    F3 after Enter: filter_mode={app._filter_mode} "
                  f"filter_text={app.filter_text!r} input_hidden={input_hidden} "
                  f"top_selected={top_selected}")
            check("F3.enter_locks_filter",
                  locked_applied and cmd_mode and input_hidden and top_selected,
                  f"(applied={locked_applied} cmd={cmd_mode} "
                  f"hidden={input_hidden} top={top_selected})")

            # ---- F4: Esc from filter CANCELS (clears) ----
            # re-enter filter mode, type, then Esc.
            await pilot.press("slash")
            await pilot.pause()
            for ch in "gamma":
                await pilot.press(ch)
            await pilot.pause()
            mid_n = len(app._filtered)
            await pilot.press("escape")
            await pilot.pause()
            cleared = app.filter_text == ""
            restored = len(app._filtered) == total
            cmd_after_esc = app._filter_mode is False
            print(f"    F4 typed 'gamma' (n={mid_n}) then Esc: "
                  f"filter_text={app.filter_text!r} filtered={len(app._filtered)} "
                  f"filter_mode={app._filter_mode}")
            check("F4.esc_cancels_filter",
                  cleared and restored and cmd_after_esc,
                  f"(cleared={cleared} restored={restored})")

            # re-select first agent for the reply tests
            app._selected_key = app._key(app._filtered[0])
            app._refresh_selection_classes()
            await pilot.pause()
            sel = app.selected_agent
            expect_pane = sel.active_pane
            print(f"    selected for reply        : "
                  f"{sel.pane_title} (pane {expect_pane})")

            # ---- R1: `r` opens the ReplyScreen ----
            stack_before = len(app.screen_stack)
            await pilot.press("r")
            await pilot.pause()
            reply_open = (len(app.screen_stack) == stack_before + 1
                          and isinstance(app.screen, ReplyScreen))
            print(f"    R1 after 'r': reply_open={reply_open}")
            check("R1.r_opens_reply_modal", reply_open,
                  f"(open={reply_open})")

            # ---- R2: typing + Shift+Enter newline ----
            ta = app.screen.query_one("#replyinput", TextArea)
            for ch in "line1":
                await pilot.press(ch)
            await pilot.press("shift+enter")
            for ch in "line2":
                await pilot.press(ch)
            await pilot.pause()
            draft = ta.text
            multiline = "\n" in draft and draft == "line1\nline2"
            print(f"    R2 draft (repr): {draft!r} multiline={multiline}")
            check("R2.shift_enter_inserts_newline", multiline,
                  f"(draft={draft!r})")

            # ---- R3: Enter SENDS with exact text + pane, then dismisses ----
            sent.clear()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            called_once = len(sent) == 1
            correct_args = called_once and sent[0] == (expect_pane, "line1\nline2")
            dismissed = not isinstance(app.screen, ReplyScreen)
            print(f"    R3 after Enter: send_calls={len(sent)} "
                  f"args={sent[0] if sent else None} dismissed={dismissed}")
            check("R3.enter_sends_and_dismisses",
                  correct_args and dismissed,
                  f"(args={sent[0] if sent else None} dismissed={dismissed})")

            # ---- R4: Esc cancels WITHOUT sending ----
            sent.clear()
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, ReplyScreen), "reply modal should open"
            ta = app.screen.query_one("#replyinput", TextArea)
            for ch in "draft":
                await pilot.press(ch)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            no_send = len(sent) == 0
            cancelled = not isinstance(app.screen, ReplyScreen)
            print(f"    R4 after Esc: send_calls={len(sent)} cancelled={cancelled}")
            check("R4.esc_cancels_without_send",
                  no_send and cancelled,
                  f"(no_send={no_send} cancelled={cancelled})")

            # ---- R5: `r` with nothing selected is a no-op ----
            saved_sel = app._selected_key
            app._selected_key = None
            stack_b = len(app.screen_stack)
            await pilot.press("r")
            await pilot.pause()
            noop = len(app.screen_stack) == stack_b and not isinstance(
                app.screen, ReplyScreen)
            print(f"    R5 r with no selection: modal_pushed={not noop}")
            check("R5.r_no_selection_noop", noop, f"(noop={noop})")
            app._selected_key = saved_sel
            app._refresh_selection_classes()
            await pilot.pause()

            # ---- T1: Tab still switches tabs ----
            from textual.widgets import TabbedContent
            tabs = app.query_one("#tabs", TabbedContent)
            before_tab = tabs.active
            await pilot.press("tab")
            await pilot.pause()
            after_tab = tabs.active
            switched = before_tab != after_tab
            print(f"    T1 Tab: {before_tab} -> {after_tab} switched={switched}")
            check("T1.tab_switches_tabs", switched,
                  f"({before_tab}->{after_tab})")
            # switch back to the agents tab for the kill regression
            await pilot.press("tab")
            await pilot.pause()

            # ---- K1: kill modal still opens on Backspace ----
            # guard self-session so the kill flow reaches the modal.
            data.current_tmux_session = lambda: "__not_a_real_session__"
            data.kill_session = lambda s: True  # stub: never actually kills
            app._selected_key = app._key(app._filtered[0])
            app._refresh_selection_classes()
            await pilot.pause()
            stack_b = len(app.screen_stack)
            await pilot.press("backspace")
            await pilot.pause()
            kill_open = (len(app.screen_stack) == stack_b + 1
                         and isinstance(app.screen, KillConfirmScreen))
            print(f"    K1 backspace: kill_modal_open={kill_open}")
            check("K1.kill_modal_still_opens", kill_open,
                  f"(open={kill_open})")
            # cancel the kill modal cleanly
            if kill_open:
                await pilot.press("n")
                await pilot.pause()

    finally:
        data.send_message_to_pane = orig_send

    # ----- report -----
    print("\n===== RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
