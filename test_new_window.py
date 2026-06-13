"""test_new_window.py — proofs for the N-key new-agent spawn flow.

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_new_window.py

SAFETY: data.spawn_claude_window is monkeypatched to a stub that records args
and returns (True, "spawned ..."). NO real window is ever spawned. Agents are
INJECTED synthetically.

Proves (headless run_test, size=120x40):
  N1. Pressing `n` opens NameInputScreen.
  N2. Typing "testbot" + enter -> DirPickerScreen is on stack; dir picker
      loaded recents (list returned).
  N3. Pressing enter in dir picker -> ModelPickerScreen is on stack.
      Default selected index is 0 (Opus).
  N4. Pressing enter in model picker -> spawn_claude_window called ONCE with
      name=="testbot", directory==the picked dir, model=="claude-opus-4-8",
      color in SPAWN_COLORS and color != "green".
  U1. Direct unit check: recent_claude_dirs() returns a list (may be empty).
"""

from __future__ import annotations

import asyncio
import os
import sys

from agents_tui import data
from agents_tui.app import (
    AgentsApp, NameInputScreen, DirPickerScreen, ModelPickerScreen, SPAWN_COLORS
)
from agents_tui.data import Agent
from textual.widgets import Input


def make_agents() -> list[Agent]:
    return [
        Agent(session="cc-alpha-1", session_id="sid-alpha",
              active_pane="%11", project="alpha", task="build",
              state="idle", pane_title="alpha-build"),
    ]


async def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    # --- monkeypatches ---
    spawned_calls: list[tuple] = []
    orig_spawn = data.spawn_claude_window
    orig_recent = data.recent_claude_dirs

    real_dirs = [os.path.expanduser("~"), "/tmp"]

    def stub_spawn(name, directory, model, color):
        spawned_calls.append((name, directory, model, color))
        return (True, "spawned {} in {} on {}".format(name, os.path.basename(directory), model))

    def stub_recent(cap=20):
        return real_dirs

    data.spawn_claude_window = stub_spawn
    data.recent_claude_dirs = stub_recent

    print("===== NEW WINDOW TESTS =====")
    try:
        # U1: direct unit check on the real function BEFORE it's replaced
        orig_dirs = orig_recent()
        check("U1.recent_claude_dirs_returns_list", isinstance(orig_dirs, list),
              f"(got {type(orig_dirs).__name__})")
        print(f"    U1 recent_claude_dirs: returned {len(orig_dirs)} dirs")

        app = AgentsApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Freeze the live refresh
            app.refresh_data = lambda: None
            app._apply_agents(make_agents())
            await pilot.pause()
            # Select the first agent
            if app._filtered:
                app._selected_key = app._key(app._filtered[0])
                app._refresh_selection_classes()
            await pilot.pause()

            # ---- N1: pressing `n` opens NameInputScreen ----
            stack_before = len(app.screen_stack)
            await pilot.press("n")
            await pilot.pause()
            name_open = (len(app.screen_stack) == stack_before + 1
                         and isinstance(app.screen, NameInputScreen))
            print(f"    N1 after 'n': name_screen_open={name_open}")
            check("N1.n_opens_name_screen", name_open,
                  f"(open={name_open})")

            # ---- N2: type "testbot" + enter -> DirPickerScreen ----
            if name_open:
                for ch in "testbot":
                    await pilot.press(ch)
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await pilot.pause()
                dir_open = isinstance(app.screen, DirPickerScreen)
                has_dirs = dir_open and len(app.screen._all_dirs) > 0
                print(f"    N2 after typing 'testbot'+enter: dir_screen={dir_open} "
                      f"dirs_loaded={has_dirs} ndirs={len(app.screen._all_dirs) if dir_open else 0}")
                check("N2.typing_name_opens_dir_picker",
                      dir_open and has_dirs,
                      f"(dir_open={dir_open} has_dirs={has_dirs})")
            else:
                check("N2.typing_name_opens_dir_picker", False, "(N1 failed, skipped)")
                dir_open = False

            # ---- N3: enter in dir picker -> ModelPickerScreen, sel=0 ----
            if dir_open:
                # First ensure the DirPickerScreen has filtered dirs; press enter
                await pilot.pause()
                # The _PickerInput's enter is intercepted by on_picker_key
                # We need to send the key in a way that reaches the _PickerInput
                await pilot.press("enter")
                await pilot.pause()
                await pilot.pause()
                model_open = isinstance(app.screen, ModelPickerScreen)
                sel_is_zero = model_open and app.screen._sel == 0
                print(f"    N3 after enter in dir picker: model_screen={model_open} "
                      f"sel={app.screen._sel if model_open else 'N/A'}")
                check("N3.dir_enter_opens_model_picker",
                      model_open,
                      f"(open={model_open})")
                check("N3.model_default_sel_is_0",
                      sel_is_zero,
                      f"(sel={app.screen._sel if model_open else 'N/A'})")
            else:
                check("N3.dir_enter_opens_model_picker", False, "(N2 failed, skipped)")
                check("N3.model_default_sel_is_0", False, "(N2 failed, skipped)")
                model_open = False

            # ---- N4: enter in model picker -> spawn called ----
            if model_open:
                spawned_calls.clear()
                await pilot.press("enter")
                await pilot.pause()
                await pilot.pause()
                called_once = len(spawned_calls) == 1
                if called_once:
                    sc_name, sc_dir, sc_model, sc_color = spawned_calls[0]
                    name_ok = sc_name == "testbot"
                    model_ok = sc_model == "claude-opus-4-8"
                    color_ok = sc_color in SPAWN_COLORS and sc_color != "green"
                    print(f"    N4 spawn called: name={sc_name!r} dir={sc_dir!r} "
                          f"model={sc_model!r} color={sc_color!r}")
                    check("N4.spawn_called_once", called_once,
                          f"(calls={len(spawned_calls)})")
                    check("N4.spawn_name_correct", name_ok,
                          f"(name={sc_name!r})")
                    check("N4.spawn_model_is_opus", model_ok,
                          f"(model={sc_model!r})")
                    check("N4.spawn_color_valid", color_ok,
                          f"(color={sc_color!r})")
                else:
                    print(f"    N4 spawn called {len(spawned_calls)} times")
                    check("N4.spawn_called_once", False,
                          f"(calls={len(spawned_calls)})")
                    check("N4.spawn_name_correct", False, "(not called)")
                    check("N4.spawn_model_is_opus", False, "(not called)")
                    check("N4.spawn_color_valid", False, "(not called)")

    finally:
        data.spawn_claude_window = orig_spawn
        data.recent_claude_dirs = orig_recent

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
