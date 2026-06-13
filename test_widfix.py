"""test_widfix.py — proofs for the stale-@aerospace_wid fix.

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_widfix.py

THE BUG: pressing ⏎ on an agent whose Ghostty window was closed showed
"could not focus window <wid> (gone?)" and dead-ended, because resolve_wid
returned the stamped @aerospace_wid without checking the window still exists.

SAFETY: data.aerospace_windows / data.focus_window / data.resolve_wid /
data.attach_session_window are monkeypatched. NO real aerospace call is made and
NO real window is ever spawned. Agents are INJECTED synthetically.

Proves:
  R1. resolve_wid drops a STALE stamp (window not in the live list) -> None.
  R2. resolve_wid returns a LIVE stamp (window present in the list).
  R3. resolve_wid drops a stale stamp but recovers via a TITLE match.
  F1. App ⏎ with a resolved-but-gone wid (focus_window -> False) offers the
      launch ConfirmScreen and pressing `y` re-attaches the session ONCE.
"""

from __future__ import annotations

import asyncio
import sys

from agents_tui import data
from agents_tui.app import AgentsApp, ConfirmScreen
from agents_tui.data import Agent, resolve_wid


def run_unit() -> tuple[list[str], list[str]]:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    orig_windows = data.aerospace_windows
    print("===== RESOLVE_WID UNIT TESTS =====")
    try:
        # ---- R1: stale stamp dropped (no live windows) -> None ----
        data.aerospace_windows = lambda: []
        ag = Agent(session="cc-x-1", active_pane=None,
                   pane_title="refactor-message-block", aerospace_wid="14473")
        r1 = resolve_wid(ag)
        print(f"    R1 stale stamp, no windows: resolve_wid -> {r1!r}")
        check("R1.stale_stamp_dropped", r1 is None, f"(got {r1!r})")

        # ---- R2: live stamp returned ----
        data.aerospace_windows = lambda: [("14473", "Ghostty", "whatever")]
        ag = Agent(session="cc-x-1", active_pane=None,
                   pane_title="refactor-message-block", aerospace_wid="14473")
        r2 = resolve_wid(ag)
        print(f"    R2 live stamp present: resolve_wid -> {r2!r}")
        check("R2.live_stamp_returned", r2 == "14473", f"(got {r2!r})")

        # ---- R3: stale stamp dropped, but title matches a live window ----
        data.aerospace_windows = lambda: [("222", "Ghostty", "myagent")]
        ag = Agent(session="cc-x-1", active_pane=None,
                   pane_title="myagent", aerospace_wid="14473")
        r3 = resolve_wid(ag)
        print(f"    R3 stale stamp + title match: resolve_wid -> {r3!r}")
        check("R3.title_match_after_stale", r3 == "222", f"(got {r3!r})")
    finally:
        data.aerospace_windows = orig_windows

    return passed, failed


def make_agents() -> list[Agent]:
    return [
        Agent(session="cc-gone-1", session_id="sid-gone",
              active_pane="%7", project="proj", task="branch",
              state="idle", aerospace_wid="123", pane_title="proj-branch"),
    ]


async def run_app() -> tuple[list[str], list[str]]:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    # --- monkeypatches ---
    attach_calls: list[tuple] = []
    orig_attach = data.attach_session_window
    orig_resolve = data.resolve_wid
    orig_focus = data.focus_window

    def stub_attach(session):
        attach_calls.append((session,))
        return (True, "opening {} in a new window".format(session))

    data.resolve_wid = lambda a, windows=None: "123"  # resolves to a wid...
    data.focus_window = lambda wid: False              # ...but focus says GONE
    data.attach_session_window = stub_attach

    print("\n===== FOCUS-FAILURE FALLBACK TEST =====")
    try:
        app = AgentsApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Freeze the live refresh
            app.refresh_data = lambda: None
            app._apply_agents(make_agents())
            await pilot.pause()
            # Select the (only) agent
            if app._filtered:
                app._selected_key = app._key(app._filtered[0])
                app._refresh_selection_classes()
            await pilot.pause()

            # ---- F1a: ⏎ with a gone wid -> launch ConfirmScreen on the stack ----
            stack_before = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            confirm_open = (len(app.screen_stack) == stack_before + 1
                            and isinstance(app.screen, ConfirmScreen))
            print(f"    F1 after 'enter' (focus failed): confirm_screen_open={confirm_open}")
            check("F1.focus_fail_offers_launch", confirm_open,
                  f"(open={confirm_open})")

            # ---- F1b: pressing `y` -> attach called once with right session ----
            if confirm_open:
                attach_calls.clear()
                await pilot.press("y")
                await pilot.pause()
                await pilot.pause()
                called_once = len(attach_calls) == 1
                session_ok = called_once and attach_calls[0][0] == "cc-gone-1"
                print(f"    F1 after 'y': attach_calls={attach_calls}")
                check("F1.y_calls_attach_once", called_once,
                      f"(calls={len(attach_calls)})")
                check("F1.attach_session_correct", session_ok,
                      f"(session={attach_calls[0][0] if called_once else 'N/A'!r})")
            else:
                check("F1.y_calls_attach_once", False, "(F1a failed, skipped)")
                check("F1.attach_session_correct", False, "(F1a failed, skipped)")
    finally:
        data.attach_session_window = orig_attach
        data.resolve_wid = orig_resolve
        data.focus_window = orig_focus

    return passed, failed


async def run() -> int:
    passed, failed = run_unit()
    p2, f2 = await run_app()
    passed += p2
    failed += f2

    print("\n===== RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
