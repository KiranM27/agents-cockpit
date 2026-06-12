"""test_kill.py — proofs for the kill-session feature (DESTRUCTIVE path).

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_kill.py

SAFETY (read before touching this file):
  - This test creates a UNIQUELY-named THROWAWAY tmux session `_kill_test` and
    ONLY EVER targets that session for a real kill. It NEVER kills any real
    (non-`_kill_test`) session. The self-guard assertion is done by monkeypatch
    so even a hypothetical guard failure could only ever touch `_kill_test`.
  - A finally-block cleans up `_kill_test` if it survives, so the test is
    idempotent and leaves no scratch sessions behind.

Proves:
  K1. MODAL BLOCKS: with the filter EMPTY, selecting the `_kill_test` row and
      pressing Backspace pushes the KillConfirmScreen AND `_kill_test` is STILL
      ALIVE (the modal blocks until confirmed — no kill yet).
  K2. CONFIRM KILLS: pressing `y` on the modal dismisses it and kills the
      scratch session — `_kill_test` is GONE from `tmux list-sessions`.
  K3. DISAMBIGUATION: with a NON-EMPTY filter, Backspace EDITS the filter
      (drops its last char) and does NOT open the kill modal / kill anything.
  K4. SELF-GUARD: when the selected agent's `.session` == current_tmux_session()
      (monkeypatched to the scratch session), Backspace shows the guard toast,
      pushes NO modal, and does NOT kill. (Targets ONLY the scratch session.)
  K5. CANCEL PATHS: `esc` and `n` cancel the modal without killing.

No transcript CONTENT of any session is printed (only session names + facts).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

from agents_tui import data
from agents_tui.app import AgentsApp, KillConfirmScreen
from agents_tui.data import Agent

SCRATCH = "_kill_test"


# --------------------------------------------------------------------------
# tmux scratch helpers — ONLY ever touch the SCRATCH session
# --------------------------------------------------------------------------

def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def list_sessions() -> set[str]:
    out = _tmux("list-sessions", "-F", "#{session_name}").stdout
    return {s.strip() for s in out.splitlines() if s.strip()}


def scratch_alive() -> bool:
    return SCRATCH in list_sessions()


def create_scratch() -> None:
    # if a leftover exists, kill ONLY the scratch first, then re-create.
    if scratch_alive():
        _tmux("kill-session", "-t", SCRATCH)
    _tmux("new-session", "-d", "-s", SCRATCH)


def cleanup_scratch() -> None:
    if scratch_alive():
        _tmux("kill-session", "-t", SCRATCH)


def make_scratch_agent() -> Agent:
    """An Agent whose .session is the scratch session (display name distinct)."""
    return Agent(session=SCRATCH, project="scratch", task="kill-test")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

async def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    real_before = list_sessions() - {SCRATCH}

    create_scratch()
    print("===== KILL FEATURE TESTS =====")
    print(f"    scratch '{SCRATCH}' created : {scratch_alive()}")
    if not scratch_alive():
        print("FATAL: could not create scratch session (is tmux running?)")
        return 1

    # Save originals we monkeypatch so we can restore.
    orig_current = data.current_tmux_session
    # also patch the symbol the app module imported via `from . import data`
    # (app uses data.current_tmux_session / data.kill_session — same module
    # object — so patching data.* is sufficient).

    try:
        app = AgentsApp()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            # wait for the first gather to land
            for _ in range(40):
                if app.agents:
                    break
                await asyncio.sleep(0.1)
            await pilot.pause()

            # The scratch session is a REAL tmux session, so it should appear
            # in the gathered list. Make sure it did; if not, inject it so the
            # selection-based assertions still run (we still only kill scratch).
            def scratch_key() -> str | None:
                for a in app._filtered:
                    if a.session == SCRATCH:
                        return app._key(a)
                return None

            sk = scratch_key()
            if sk is None:
                # inject the scratch agent into the app's view directly.
                inj = make_scratch_agent()
                app._apply_agents(list(app.agents) + [inj])
                await pilot.pause()
                sk = scratch_key()
            print(f"    scratch present in list   : {sk is not None}")
            check("K0.scratch_in_list", sk is not None,
                  f"(key={sk})")

            # Ensure self-guard does NOT fire for the real test: force
            # current_tmux_session to something that is NOT the scratch session.
            data.current_tmux_session = lambda: "__not_scratch__"

            # ---- K3: DISAMBIGUATION — non-empty filter, Backspace edits ----
            # Do this FIRST (no real session ever targeted: a kill won't fire).
            app.filter_text = "abc"
            app._update_filter_display()
            app._rebuild_list()
            await pilot.pause()
            sess_before_k3 = list_sessions()
            stack_before_k3 = len(app.screen_stack)
            await pilot.press("backspace")
            await pilot.pause()
            edited = app.filter_text == "ab"
            no_modal_k3 = len(app.screen_stack) == stack_before_k3
            no_kill_k3 = list_sessions() == sess_before_k3
            print(f"    K3 filter 'abc'->'{app.filter_text}' "
                  f"(edited={edited}) modal_opened={not no_modal_k3} "
                  f"killed_anything={not no_kill_k3}")
            check("K3.nonempty_filter_backspace_edits_not_kill",
                  edited and no_modal_k3 and no_kill_k3,
                  f"(filter={app.filter_text!r})")
            # clear the filter back to empty for the remaining tests
            app.filter_text = ""
            app._update_filter_display()
            app._rebuild_list()
            await pilot.pause()

            # re-resolve scratch key (list may have changed) and SELECT it
            sk = scratch_key()
            if sk is None:
                inj = make_scratch_agent()
                app._apply_agents(list(app.agents) + [inj])
                await pilot.pause()
                sk = scratch_key()
            app._selected_key = sk
            app._refresh_selection_classes()
            await pilot.pause()
            sel = app.selected_agent
            print(f"    selected agent session    : "
                  f"{sel.session if sel else None}")
            check("K0b.scratch_selected",
                  sel is not None and sel.session == SCRATCH,
                  f"(session={sel.session if sel else None})")

            # ---- K4: SELF-GUARD — pretend scratch IS the cockpit's session ----
            # Monkeypatch current_tmux_session to RETURN the scratch session so
            # the guard sees selected.session == self-session. Even if the guard
            # failed, only the scratch session would be at risk — but it must
            # NOT kill and must NOT open a modal.
            data.current_tmux_session = lambda: SCRATCH
            sess_before_k4 = list_sessions()
            stack_before_k4 = len(app.screen_stack)
            assert scratch_alive(), "scratch must be alive before self-guard test"
            await pilot.press("backspace")
            await pilot.pause()
            guard_no_modal = len(app.screen_stack) == stack_before_k4
            guard_no_kill = scratch_alive() and list_sessions() == sess_before_k4
            print(f"    K4 self-guard: modal_opened={not guard_no_modal} "
                  f"scratch_alive={scratch_alive()} "
                  f"killed_anything={not guard_no_kill}")
            check("K4.self_guard_refuses_kill",
                  guard_no_modal and guard_no_kill,
                  f"(no_modal={guard_no_modal} no_kill={guard_no_kill})")

            # restore: self-session is NOT the scratch session now
            data.current_tmux_session = lambda: "__not_scratch__"

            # ---- K5: CANCEL PATHS (esc, n) do NOT kill ----
            for cancel_key in ("escape", "n"):
                stack_b = len(app.screen_stack)
                await pilot.press("backspace")
                await pilot.pause()
                opened = (len(app.screen_stack) == stack_b + 1
                          and isinstance(app.screen, KillConfirmScreen))
                alive_during = scratch_alive()
                await pilot.press(cancel_key)
                await pilot.pause()
                closed = len(app.screen_stack) == stack_b
                still_alive = scratch_alive()
                print(f"    K5 cancel via '{cancel_key}': opened={opened} "
                      f"alive_during={alive_during} closed={closed} "
                      f"still_alive={still_alive}")
                check(f"K5.cancel_{cancel_key}_no_kill",
                      opened and alive_during and closed and still_alive,
                      f"(opened={opened} closed={closed} alive={still_alive})")

            # ---- K1: MODAL BLOCKS — Backspace opens modal, NOT yet killed ----
            assert app.filter_text == "", "filter must be empty for K1"
            stack_before = len(app.screen_stack)
            await pilot.press("backspace")
            await pilot.pause()
            modal_open = (len(app.screen_stack) == stack_before + 1
                          and isinstance(app.screen, KillConfirmScreen))
            alive_with_modal = scratch_alive()
            print(f"    K1 modal opened={modal_open} "
                  f"scratch_alive_with_modal_open={alive_with_modal}")
            check("K1.modal_blocks_until_confirmed",
                  modal_open and alive_with_modal,
                  f"(modal={modal_open} alive={alive_with_modal})")

            # verify the modal shows BOTH the display name and the tmux session.
            # The display name is the selected agent's card title (pane_title ->
            # label) — read it off the live agent rather than
            # assuming, since the scratch session is a real gathered session.
            from agents_tui.app import AgentRow as _AR
            sel_now = app.selected_agent
            expect_name = (_AR._card_title(sel_now) if sel_now else "")
            modal = app.screen
            body_txt = ""
            if isinstance(modal, KillConfirmScreen):
                from textual.widgets import Static as _S
                try:
                    w = modal.query_one("#killbody", _S)
                    r = w.render()
                    body_txt = r.plain if hasattr(r, "plain") else str(r)
                except Exception:
                    body_txt = ""
            shows_both = (bool(expect_name) and expect_name in body_txt
                          and SCRATCH in body_txt)
            print(f"    K1b modal shows name+session : {shows_both} "
                  f"(name={expect_name!r})")
            check("K1b.modal_shows_name_and_session", shows_both,
                  f"(name={expect_name!r} session={SCRATCH})")

            # ---- K2: CONFIRM KILLS — press `y`, scratch goes away ----
            await pilot.press("y")
            await pilot.pause()
            await pilot.pause()
            # give the kill a beat to take effect
            for _ in range(20):
                if not scratch_alive():
                    break
                await asyncio.sleep(0.1)
            await pilot.pause()
            gone = not scratch_alive()
            modal_closed = not isinstance(app.screen, KillConfirmScreen)
            print(f"    K2 after 'y': scratch_gone={gone} "
                  f"modal_closed={modal_closed}")
            check("K2.confirm_kills_scratch", gone and modal_closed,
                  f"(gone={gone} modal_closed={modal_closed})")

    finally:
        data.current_tmux_session = orig_current
        cleanup_scratch()

    # ---- final safety audit: no REAL session was touched ----
    real_after = list_sessions() - {SCRATCH}
    scratch_left = scratch_alive()
    same_real = real_before == real_after
    print("\n===== SAFETY AUDIT =====")
    print(f"    real sessions unchanged   : {same_real}")
    if not same_real:
        print(f"      before : {sorted(real_before)}")
        print(f"      after  : {sorted(real_after)}")
    print(f"    scratch cleaned up        : {not scratch_left}")
    check("SAFE.no_real_session_touched", same_real,
          "(real session set identical before/after)")
    check("SAFE.scratch_cleaned_up", not scratch_left,
          "(no leftover _kill_test)")

    # ----- report -----
    print("\n===== KILL TEST RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
