"""test_ctx_reset.py — proofs for the Context-tab "Clear monitor error" flow.

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_ctx_reset.py

SAFETY: NO real monitor-state.json / reset-flag I/O EVER happens.
`data.load_monitor_states_by_sid` is monkeypatched to return a deterministic
{sid: state} map so exactly ONE injected row is forced into ERROR without any
real file on disk, and `data.request_state_reset` is monkeypatched to a stub
that RECORDS its sid args instead of writing a flag the live daemon would pick
up. Agents are INJECTED synthetically so the proofs don't depend on the live
fleet, and the live refresh timer is frozen.

Proves (headless run_test, size=120x40):
  C1. Tab switches to the Context tab (agents-tab -> context-tab).
  C2. Rows are selectable and the default selection lands on the ERROR row.
  C3. `down` moves the Context selection (selected key changes).
  C4. Enter on the ERROR row opens a ConfirmScreen; `y` calls request_state_reset
      ONCE with the error sid, then dismisses the modal.
  C5. Enter on a NON-error row is a no-op: no ConfirmScreen pushed, no reset call.

No transcript CONTENT is printed.
"""

from __future__ import annotations

import asyncio
import sys

from agents_tui import data
from agents_tui.app import AgentsApp, ConfirmScreen
from agents_tui.data import Agent
from textual.widgets import TabbedContent


def make_agents() -> list[Agent]:
    """Three synthetic agents with distinct labels / panes / sids, all idle."""
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

    # ---- monkeypatch the file/daemon boundary deterministically ----
    orig_load = data.load_monitor_states_by_sid
    orig_reset = data.request_state_reset

    # Force EXACTLY one row (sid-beta) into ERROR with NO real monitor-state.json.
    data.load_monitor_states_by_sid = lambda: {"sid-beta": "ERROR"}

    # Stub the reset: record sid, never write a flag the live daemon would read.
    calls: list[str] = []

    def stub_reset(sid: str):
        calls.append(sid)
        return (True, "ok")

    data.request_state_reset = stub_reset

    print("===== CTX RESET TESTS =====")
    try:
        app = AgentsApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Freeze the live refresh so injected synthetic agents persist.
            app.refresh_data = lambda: None
            # Inject synthetic agents so proofs don't depend on the live fleet.
            app._apply_agents(make_agents())
            await pilot.pause()

            # ---- C1: Tab switches to the Context tab ----
            tabs = app.query_one("#tabs", TabbedContent)
            before_tab = tabs.active
            await pilot.press("tab")
            await pilot.pause()
            on_context = tabs.active == "context-tab"
            print(f"    C1 Tab: {before_tab} -> {tabs.active}")
            check("C1.tab_switches_to_context", on_context,
                  f"({before_tab}->{tabs.active})")

            # Compute the expected key for the beta(error) row the SAME way the
            # app does: find the row whose .sid == "sid-beta" and call _ctx_key.
            beta_row = next((r for r in app._ctx_rows if r.sid == "sid-beta"),
                            None)
            alpha_row = next((r for r in app._ctx_rows if r.sid == "sid-alpha"),
                             None)
            beta_key = app._ctx_key(beta_row) if beta_row is not None else None
            alpha_key = app._ctx_key(alpha_row) if alpha_row is not None else None

            # ---- C2: rows selectable; default selection on the ERROR row ----
            rows_nonempty = bool(app._ctx_rows)
            default_on_error = app._ctx_selected_key == beta_key
            print(f"    C2 rows={len(app._ctx_rows)} sel={app._ctx_selected_key!r} "
                  f"beta_key={beta_key!r} is_error={getattr(beta_row, 'is_error', None)}")
            check("C2.rows_selectable_default_error",
                  rows_nonempty and beta_key is not None and default_on_error,
                  f"(n={len(app._ctx_rows)} sel={app._ctx_selected_key!r} "
                  f"beta={beta_key!r})")

            # ---- C3: `down` moves the selection ----
            key_before = app._ctx_selected_key
            await pilot.press("down")
            await pilot.pause()
            moved = app._ctx_selected_key != key_before
            print(f"    C3 down: {key_before!r} -> {app._ctx_selected_key!r} "
                  f"moved={moved}")
            check("C3.move_changes_selection", moved,
                  f"({key_before!r}->{app._ctx_selected_key!r})")
            # return selection up (best-effort symmetry)
            await pilot.press("up")
            await pilot.pause()

            # ---- C4: Enter on the ERROR row opens confirm + resets ----
            calls.clear()
            app._ctx_selected_key = beta_key
            app._refresh_ctx_selection_classes()
            await pilot.pause()
            stack_before = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            confirm_open = (isinstance(app.screen, ConfirmScreen)
                            and len(app.screen_stack) == stack_before + 1)
            print(f"    C4a enter on error: confirm_open={confirm_open} "
                  f"stack {stack_before}->{len(app.screen_stack)}")
            # confirm with `y`
            await pilot.press("y")
            await pilot.pause()
            await pilot.pause()
            reset_once = calls == ["sid-beta"]
            dismissed = not isinstance(app.screen, ConfirmScreen)
            print(f"    C4b after 'y': calls={calls} dismissed={dismissed}")
            check("C4.enter_on_error_opens_confirm_and_resets",
                  confirm_open and reset_once and dismissed,
                  f"(open={confirm_open} calls={calls} dismissed={dismissed})")

            # ---- C5: Enter on a NON-error row is a no-op ----
            calls.clear()
            app._ctx_selected_key = alpha_key
            app._refresh_ctx_selection_classes()
            await pilot.pause()
            stack_before = len(app.screen_stack)
            await pilot.press("enter")
            await pilot.pause()
            no_modal = (len(app.screen_stack) == stack_before
                        and not isinstance(app.screen, ConfirmScreen))
            no_reset = calls == []
            print(f"    C5 enter on non-error: no_modal={no_modal} "
                  f"calls={calls}")
            check("C5.enter_on_non_error_noop", no_modal and no_reset,
                  f"(no_modal={no_modal} calls={calls})")

    finally:
        data.load_monitor_states_by_sid = orig_load
        data.request_state_reset = orig_reset

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
