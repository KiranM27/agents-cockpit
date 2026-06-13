"""test_reattach_window.py — proofs for the ⏎-on-windowless re-attach flow.

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_reattach_window.py

SAFETY: data.attach_session_window is monkeypatched to a stub that records args
and returns (True, "opening ..."). NO real window is ever spawned. data.resolve_wid
is forced to None so the test never depends on aerospace state. The agent is
INJECTED synthetically.

Proves (headless run_test, size=120x40):
  A. Pressing ⏎ on a windowless agent -> ConfirmScreen is on the screen stack.
  B. Pressing `y` on the ConfirmScreen -> attach_session_window called ONCE with
     session == "cc-detached-1".
  C. Pressing `n` (cancel) on the ConfirmScreen does NOT call attach_session_window.
"""

from __future__ import annotations

import asyncio
import sys

from agents_tui import data
from agents_tui.app import AgentsApp, ConfirmScreen
from agents_tui.data import Agent


def make_agents() -> list[Agent]:
    return [
        Agent(session="cc-detached-1", session_id="sid-det",
              active_pane="%9", project="proj", task="branch",
              state="idle", aerospace_wid=None, pane_title="proj-branch"),
    ]


async def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    # --- monkeypatches ---
    attach_calls: list[tuple] = []
    orig_attach = data.attach_session_window
    orig_resolve = data.resolve_wid

    def stub_attach(session):
        attach_calls.append((session,))
        return (True, "opening {} in a new window".format(session))

    def stub_resolve(a):
        return None  # force the no-window path; never touch aerospace

    data.attach_session_window = stub_attach
    data.resolve_wid = stub_resolve

    print("===== REATTACH WINDOW TESTS =====")
    try:
        app = AgentsApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Freeze the live refresh
            app.refresh_data = lambda: None
            app._apply_agents(make_agents())
            await pilot.pause()
            # Select the (only) windowless agent
            if app._filtered:
                app._selected_key = app._key(app._filtered[0])
                app._refresh_selection_classes()
            await pilot.pause()

            # ---- A: pressing ⏎ opens ConfirmScreen ----
            stack_before = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            confirm_open = (len(app.screen_stack) == stack_before + 1
                            and isinstance(app.screen, ConfirmScreen))
            print(f"    A after 'enter': confirm_screen_open={confirm_open}")
            check("A.enter_opens_confirm_screen", confirm_open,
                  f"(open={confirm_open})")

            # ---- B: pressing `y` -> attach called once with right session ----
            if confirm_open:
                attach_calls.clear()
                await pilot.press("y")
                await pilot.pause()
                await pilot.pause()
                called_once = len(attach_calls) == 1
                session_ok = called_once and attach_calls[0][0] == "cc-detached-1"
                print(f"    B after 'y': attach_calls={attach_calls}")
                check("B.y_calls_attach_once", called_once,
                      f"(calls={len(attach_calls)})")
                check("B.attach_session_correct", session_ok,
                      f"(session={attach_calls[0][0] if called_once else 'N/A'!r})")
            else:
                check("B.y_calls_attach_once", False, "(A failed, skipped)")
                check("B.attach_session_correct", False, "(A failed, skipped)")

            # ---- C: re-open modal, press `n` -> attach NOT called ----
            # Re-open by pressing enter again on the still-selected agent.
            await pilot.press("enter")
            await pilot.pause()
            reopened = isinstance(app.screen, ConfirmScreen)
            print(f"    C re-open confirm: confirm_screen_open={reopened}")
            if reopened:
                attach_calls.clear()
                await pilot.press("n")
                await pilot.pause()
                await pilot.pause()
                not_called = len(attach_calls) == 0
                print(f"    C after 'n': attach_calls={attach_calls}")
                check("C.n_does_not_call_attach", not_called,
                      f"(calls={len(attach_calls)})")
            else:
                check("C.n_does_not_call_attach", False, "(re-open failed, skipped)")

    finally:
        data.attach_session_window = orig_attach
        data.resolve_wid = orig_resolve

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
