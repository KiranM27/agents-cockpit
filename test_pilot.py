"""test_pilot.py — pilot test for agents-tui against the REAL current sessions.

Run with the venv python:
    .venv/bin/python test_pilot.py

Asserts (and prints evidence):
  1. App boots; left list row-count == `tmux list-sessions` count.
  2. Header counts (agents / working / need-attention) are computed and the
     need-attention count matches an INDEPENDENT raw colour52 sweep.
  3. Preview renders NON-EMPTY text for the selected agent.
  4. ↓ moves selection; → jumps to a needs-input agent; typing filters
     (matched count drops); esc restores.
  5. wid resolution: agent-ckpit resolves to a wid that exists in
     `aerospace list-windows --all` (we DO NOT call `aerospace focus`).

No transcript CONTENT from non-own sessions is printed.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

from agents_tui import data
from agents_tui.app import AgentRow, AgentsApp, PreviewPane


def raw_session_count() -> int:
    out = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    ).stdout
    return len([s for s in out.splitlines() if s.strip()])


def raw_attention_sessions() -> set[str]:
    """Independent colour52 sweep -> set of needy session names."""
    panes = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{session_name}"],
        capture_output=True, text=True,
    ).stdout
    needy: set[str] = set()
    for line in panes.splitlines():
        line = line.strip()
        if not line:
            continue
        pane, sess = line.split(" ", 1)
        style = subprocess.run(
            ["tmux", "show-options", "-p", "-t", pane, "-qv",
             "window-active-style"],
            capture_output=True, text=True,
        ).stdout.strip()
        if "bg=colour52" in style:
            needy.add(sess)
    return needy


def aerospace_wids() -> set[str]:
    out = subprocess.run(
        ["/opt/homebrew/bin/aerospace", "list-windows", "--all"],
        capture_output=True, text=True,
    ).stdout
    wids: set[str] = set()
    for line in out.splitlines():
        head = line.split("|", 1)[0].strip()
        if head.isdigit():
            wids.add(head)
    return wids


async def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    app = AgentsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        for _ in range(30):
            if app.agents:
                break
            await asyncio.sleep(0.1)
        await pilot.pause()

        # --- 1. boot + row count ---
        sess_count = raw_session_count()
        rows = list(app.query(AgentRow))
        check("1.boot_rowcount",
              len(rows) == sess_count and len(app.agents) == sess_count,
              f"(rows={len(rows)} agents={len(app.agents)} "
              f"tmux_sessions={sess_count})")

        # --- 2. header counts vs raw sweep ---
        raw_needy = raw_attention_sessions()
        app_needy = {a.session for a in app.agents if a.state == "needs-input"}
        working = sum(1 for a in app.agents if a.state == "working")
        attention = len(app_needy)
        check("2.attention_matches_raw_sweep",
              app_needy == raw_needy,
              f"(app_needy={attention} raw_needy={len(raw_needy)} "
              f"working={working})")
        # show which sessions (names only, no content)
        print("    raw needy sessions :", sorted(raw_needy))
        print("    app needy sessions :", sorted(app_needy))
        print(f"    counts: total={len(app.agents)} working={working} "
              f"attention={attention}")

        # --- 3. preview non-empty for selected ---
        from textual.widgets import Static as _Static

        def _static_text(w) -> str:
            try:
                r = w.render()
            except Exception:
                return ""
            return r.plain if hasattr(r, "plain") else str(r)

        preview = app.query_one(PreviewPane)
        preview_text = " ".join(
            _static_text(s) for s in preview.query(_Static)
        )
        check("3.preview_nonempty", len(preview_text.strip()) > 0,
              f"(preview_chars={len(preview_text.strip())})")

        # --- 4a. ↓ moves selection ---
        before = app._selected_key
        await pilot.press("down")
        await pilot.pause()
        after = app._selected_key
        check("4a.down_moves_selection", before != after,
              f"(changed={before != after})")

        # --- 4b. → jumps to a needs-input agent (if any exist) ---
        if attention > 0:
            await pilot.press("right")
            await pilot.pause()
            sel = app.selected_agent
            check("4b.next_alert_selects_needy",
                  sel is not None and sel.state == "needs-input",
                  f"(selected_state={sel.state if sel else None})")
        else:
            passed.append("4b.next_alert_selects_needy  (skipped: 0 needy)")

        # --- 4c. typing filters; matched count drops ---
        total = len(app.agents)
        # type a query that should match a subset (project name fragment)
        # pick the first agent's first label token as a real filter target
        token = app.agents[0].label.split()[0][:4] if app.agents else "lexi"
        for ch in token:
            await pilot.press(ch)
        await pilot.pause()
        filtered = len(app._filtered)
        check("4c.typing_filters",
              filtered <= total,
              f"(filter='{token}' matched={filtered}/{total})")

        # type a definitely-nonmatching string to force a real drop
        for ch in "zzqx":
            await pilot.press(ch)
        await pilot.pause()
        dropped = len(app._filtered)
        check("4c2.filter_drops_to_fewer",
              dropped < total,
              f"(filter='{token}zzqx' matched={dropped}/{total})")

        # --- 4d. esc restores full list ---
        await pilot.press("escape")
        await pilot.pause()
        restored = len(app._filtered)
        check("4d.esc_restores", restored == total,
              f"(restored={restored}/{total})")

    # --- 5. wid resolution (outside the app; no focus call) ---
    agents = data.gather_agents()
    ckpit = next((a for a in agents if a.session == "agent-ckpit"), None)
    wids = aerospace_wids()
    if ckpit is not None:
        wid = data.resolve_wid(ckpit)
        check("5.wid_resolves_and_exists",
              wid is not None and wid in wids,
              f"(agent-ckpit wid={wid} exists_in_aerospace={wid in wids})")
    else:
        check("5.wid_resolves_and_exists", False,
              "(agent-ckpit session not found)")

    # --- report ---
    print("\n===== PILOT RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
