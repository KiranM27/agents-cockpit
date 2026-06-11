"""test_inplace.py — regression proofs for the three agents-tui fixes.

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_inplace.py

Proves:
  1. FLICKER FIX (Issue 1) — across >=2 steady-state refresh ticks (same agent
     set, same order, same preview event count) NO AgentRow or preview body
     Static is removed/re-mounted: the same Python object id()s persist. Content
     still updates in place (mutated age/pct/clock shows through).
  2. CTX-JOIN FIX (Issue 3) — count agents with pct after the session-based
     join, vs the old pane-walk join, vs the raw ctx-file count. Session-based
     join attaches ctx% to ~all live sessions that have a ctx file.
  3. CARDS (Issue 2) — export an SVG screenshot (/tmp/agents-tui-cards.svg) and
     print a short text excerpt of a few rows showing spacing + selection.

No transcript CONTENT from non-own sessions is printed (only structural ids,
counts, and short labels).
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys

from textual.widgets import Static

from agents_tui import data
from agents_tui.app import AgentRow, AgentsApp, PreviewPane


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def raw_ctx_file_pct_count() -> int:
    """How many /tmp/claude-ctx/*.json (excluding monitor*) have non-null pct."""
    d = "/tmp/claude-ctx"
    n = 0
    try:
        files = os.listdir(d)
    except OSError:
        return 0
    for fn in files:
        if not fn.endswith(".json") or fn.startswith("monitor"):
            continue
        try:
            j = json.load(open(os.path.join(d, fn)))
        except Exception:
            continue
        if j.get("pct") is not None:
            n += 1
    return n


def old_panewalk_pct_count(panes_all, sessions) -> int:
    """Reproduce the OLD pane-walk join purely to count, for before/after."""
    ctx_by_pane = data.load_ctx_by_pane()
    sess_panes: dict[str, list[str]] = {}
    for pane, sess in panes_all:
        sess_panes.setdefault(sess, []).append(pane)
    n = 0
    for sess in sessions:
        ctx = None
        for p in sess_panes.get(sess, []):
            if p in ctx_by_pane:
                ctx = ctx_by_pane[p]
                break
        if ctx and ctx.get("pct") is not None:
            n += 1
    return n


def row_text(row: AgentRow) -> str:
    r = row.render()
    return r.plain if hasattr(r, "plain") else str(r)


def static_text(w: Static) -> str:
    try:
        r = w.render()
    except Exception:
        return ""
    return r.plain if hasattr(r, "plain") else str(r)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

async def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    # ===== Issue 3: ctx% coverage (before/after), independent of the app =====
    panes_all = data.list_panes_all()
    sessions = data.list_sessions()
    file_pct = raw_ctx_file_pct_count()
    old_pct = old_panewalk_pct_count(panes_all, sessions)
    agents_now = data.gather_agents()
    new_pct = sum(1 for a in agents_now if a.pct is not None)
    live_with_ctx = len(data.load_ctx_by_session(panes_all))

    print("===== ISSUE 3: ctx% coverage =====")
    print(f"    ctx files with pct (raw)          : {file_pct}")
    print(f"    OLD pane-walk join -> agents w/pct : {old_pct}")
    print(f"    NEW session join   -> agents w/pct : {new_pct}")
    print(f"    live sessions resolved to a ctx    : {live_with_ctx}")
    print(f"    total live tmux sessions           : {len(sessions)}")
    # New join must cover (a) every live session that has a ctx file, and
    # (b) be at least as good as the old join.
    check("3.ctx_join_covers_live_sessions",
          new_pct == live_with_ctx and new_pct >= old_pct,
          f"(new={new_pct} live_with_ctx={live_with_ctx} old={old_pct} "
          f"files={file_pct})")

    # ===== boot the app for the structural / card proofs =====
    app = AgentsApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        for _ in range(30):
            if app.agents:
                break
            await asyncio.sleep(0.1)
        await pilot.pause()

        if not app.agents:
            failed.append("0.boot  (no agents gathered)")
            print("\nFATAL: no agents — cannot run structural proofs")
        else:
            # Snapshot ONE real agent list and feed it twice for guaranteed
            # steady state (gather between ticks could differ).
            base = list(app.agents)

            # tick A: apply the snapshot
            app._apply_agents(list(base))
            await pilot.pause()

            rows_a = list(app.query(AgentRow))
            ids_a = {id(r) for r in rows_a}
            key_to_id_a = {app._key(r.agent): id(r) for r in rows_a}
            preview = app.query_one(PreviewPane)
            pre_body_ids_a = [id(w) for w in preview._body_pool]
            pre_header_ids_a = [
                id(preview._w_title), id(preview._w_chip),
                id(preview._w_meta), id(preview._w_spacer),
            ]
            sel_key = app._selected_key
            sel_event_count_a = len(preview._body_pool)

            # tick B: apply the SAME snapshot again (steady state)
            app._apply_agents(list(base))
            await pilot.pause()
            # tick C: once more, to satisfy ">=2 ticks"
            app._apply_agents(list(base))
            await pilot.pause()

            rows_c = list(app.query(AgentRow))
            ids_c = {id(r) for r in rows_c}
            key_to_id_c = {app._key(r.agent): id(r) for r in rows_c}
            pre_body_ids_c = [id(w) for w in preview._body_pool]
            pre_header_ids_c = [
                id(preview._w_title), id(preview._w_chip),
                id(preview._w_meta), id(preview._w_spacer),
            ]

            print("\n===== ISSUE 1: no-remount structural proof =====")
            print(f"    AgentRow count tick A / C : {len(rows_a)} / {len(rows_c)}")
            print(f"    AgentRow id set unchanged : {ids_a == ids_c}")
            print(f"    per-key id map unchanged  : {key_to_id_a == key_to_id_c}")
            print(f"    preview header ids same   : "
                  f"{pre_header_ids_a == pre_header_ids_c}")
            print(f"    preview body event count  : {sel_event_count_a} "
                  f"(ids same={pre_body_ids_a == pre_body_ids_c})")
            # sample evidence (first 3 row ids)
            samp = list(key_to_id_a.items())[:3]
            for k, i in samp:
                print(f"      row key={k[:18]:<18} id(A)={i} "
                      f"id(C)={key_to_id_c.get(k)}")

            check("1a.agentrow_ids_persist_across_ticks",
                  ids_a == ids_c and key_to_id_a == key_to_id_c,
                  f"(rows={len(rows_a)} all_same={ids_a == ids_c})")
            check("1b.preview_header_ids_persist",
                  pre_header_ids_a == pre_header_ids_c,
                  "(4 stable header Statics)")
            check("1c.preview_body_ids_persist_steady_state",
                  pre_body_ids_a == pre_body_ids_c,
                  f"(body_event_count={sel_event_count_a})")

            # ----- content STILL updates in place (not a frozen widget) -----
            # Mutate the selected agent's age + pct in a fed copy and confirm
            # the SAME row widget's rendered text changes.
            sel_row = next((r for r in rows_c
                            if app._key(r.agent) == sel_key), None)
            if sel_row is None:
                sel_row = rows_c[0]
                sel_key = app._key(sel_row.agent)
            sel_id_before = id(sel_row)
            text_before = row_text(sel_row)

            mutated = []
            for a in base:
                a2 = copy.copy(a)
                if app._key(a2) == sel_key:
                    a2.age_seconds = (a2.age_seconds or 0) + 12345
                    a2.pct = (a2.pct or 0) + 7 if a2.pct is not None else 77
                mutated.append(a2)
            app._apply_agents(mutated)
            await pilot.pause()

            rows_d = list(app.query(AgentRow))
            sel_row_after = next((r for r in rows_d
                                  if app._key(r.agent) == sel_key), None)
            sel_id_after = id(sel_row_after) if sel_row_after else None
            text_after = row_text(sel_row_after) if sel_row_after else ""

            print("\n===== ISSUE 1: in-place content update proof =====")
            print(f"    selected row id before/after : "
                  f"{sel_id_before} / {sel_id_after} "
                  f"(same={sel_id_before == sel_id_after})")
            print(f"    rendered text changed        : "
                  f"{text_before != text_after}")

            check("1d.content_updates_in_place_same_widget",
                  sel_id_after == sel_id_before and text_before != text_after,
                  f"(same_widget={sel_id_after == sel_id_before} "
                  f"text_changed={text_before != text_after})")

            # ===== Issue 2: cards SVG + text excerpt =====
            # restore the unmutated steady state for a clean screenshot
            app._apply_agents(list(base))
            await pilot.pause()
            try:
                svg = app.export_screenshot()
                with open("/tmp/agents-tui-cards.svg", "w") as f:
                    f.write(svg)
                wrote = os.path.getsize("/tmp/agents-tui-cards.svg") > 0
            except Exception as e:  # noqa: BLE001
                wrote = False
                print("    screenshot error:", e)
            check("2.cards_svg_exported", wrote,
                  "(/tmp/agents-tui-cards.svg)")

            print("\n===== ISSUE 2: card row text excerpt (first 3) =====")
            for r in list(app.query(AgentRow))[:3]:
                cls = " ".join(sorted(r.classes)) or "-"
                txt = row_text(r).replace("\n", " ⏎ ")
                print(f"    [{cls}]  {txt[:110]}")
            sel = app.selected_agent
            print(f"    selected agent state: "
                  f"{sel.state if sel else None}  "
                  f"(card outer height={list(app.query(AgentRow))[0].outer_size.height})")

    # ----- report -----
    print("\n===== INPLACE TEST RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
