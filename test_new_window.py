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
import tempfile

from agents_tui import data
from agents_tui.app import (
    AgentsApp, NameInputScreen, DirPickerScreen, ModelPickerScreen, SPAWN_COLORS,
    _is_worktree,
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


def run_worktree_checks() -> int:
    """Synchronous proofs for _is_worktree (Change 3). Uses tmp dirs so it's
    deterministic and never touches the real fleet."""
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    print("\n===== WORKTREE-DETECTION TESTS =====")
    with tempfile.TemporaryDirectory() as tmp:
        # (a) a linked worktree: `.git` is a FILE pointing at .../worktrees/<name>
        wt = os.path.join(tmp, "feature-worktree")
        os.makedirs(wt)
        with open(os.path.join(wt, ".git"), "w") as f:
            f.write("gitdir: /Users/kiran/Desktop/repo/.git/worktrees/feature-worktree\n")
        is_wt = _is_worktree(wt)
        print(f"    W1 worktree (.git file -> /worktrees/): _is_worktree={is_wt}")
        check("W1.worktree_detected", is_wt is True, f"(got {is_wt})")

        # (b) a normal non-git dir: no `.git` at all -> not a worktree
        plain = os.path.join(tmp, "plain")
        os.makedirs(plain)
        is_wt = _is_worktree(plain)
        print(f"    W2 plain dir (no .git): _is_worktree={is_wt}")
        check("W2.plain_dir_kept", is_wt is False, f"(got {is_wt})")

        # (c) a real repo (main checkout): `.git` is a DIRECTORY -> not a worktree
        repo = os.path.join(tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        is_wt = _is_worktree(repo)
        print(f"    W3 main repo (.git dir): _is_worktree={is_wt}")
        check("W3.main_repo_kept", is_wt is False, f"(got {is_wt})")

        # (d) a `.git` file WITHOUT the /worktrees/ marker (defensive) -> kept
        weird = os.path.join(tmp, "gitfile-no-worktree")
        os.makedirs(weird)
        with open(os.path.join(weird, ".git"), "w") as f:
            f.write("gitdir: /some/other/path/.git\n")
        is_wt = _is_worktree(weird)
        print(f"    W4 .git file w/o /worktrees/: _is_worktree={is_wt}")
        check("W4.non_worktree_gitfile_kept", is_wt is False, f"(got {is_wt})")

    print("\n===== RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


async def run_disambiguation_checks() -> int:
    """Headless proof for basename disambiguation (Change 2): given two dirs with
    the SAME basename, BOTH render their full path; a UNIQUE basename renders
    name-only. Drives the real DirPickerScreen._render_results via a pilot and
    reads the rendered #dirresults text."""
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    print("\n===== DISAMBIGUATION TESTS =====")
    # Two colliding basenames ("lexi-backend") + one unique ("frontend"). Use
    # tmp dirs that actually exist and are NOT worktrees so none get filtered.
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "alpha", "lexi-backend")
        b = os.path.join(tmp, "beta", "lexi-backend")
        c = os.path.join(tmp, "frontend")
        for d in (a, b, c):
            os.makedirs(d)
        dirs = [a, b, c]

        app = AgentsApp()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.refresh_data = lambda: None
            await pilot.pause()
            screen = DirPickerScreen(dirs)
            await app.push_screen(screen)
            await pilot.pause()
            from textual.widgets import Static
            rendered = screen.query_one("#dirresults", Static).render()
            text = rendered.plain if hasattr(rendered, "plain") else str(rendered)

        # collision counts computed once, worktrees excluded (none here)
        counts_ok = (screen._base_counts.get("lexi-backend") == 2
                     and screen._base_counts.get("frontend") == 1
                     and len(screen._all_dirs) == 3)
        print(f"    D0 base_counts={screen._base_counts} ndirs={len(screen._all_dirs)}")
        check("D0.collision_counts_correct", counts_ok, f"({screen._base_counts})")

        # colliding basename -> BOTH full paths appear in the display
        both_paths = a in text and b in text
        print(f"    D1 colliding 'lexi-backend' shows both paths: {both_paths}")
        check("D1.colliding_shows_path", both_paths,
              f"(a_in={a in text} b_in={b in text})")

        # unique basename 'frontend' -> name shows but its full path does NOT
        unique_name_only = ("frontend" in text) and (c not in text)
        print(f"    D2 unique 'frontend' name-only (no path): {unique_name_only}")
        check("D2.unique_name_only", unique_name_only,
              f"(name_in={'frontend' in text} path_in={c in text})")

    print("\n===== RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    rc_flow = asyncio.run(run())
    rc_wt = run_worktree_checks()
    rc_disambig = asyncio.run(run_disambiguation_checks())
    sys.exit(rc_flow or rc_wt or rc_disambig)
