"""test_ctx_columns.py — proofs for the Context-tab column layout (header row,
responsive width spread, ellipsis truncation).

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_ctx_columns.py

SAFETY: NO real monitor-state.json / reset-flag I/O. `load_monitor_states_by_sid`
is monkeypatched to {} so no row is forced into ERROR and no file is read. Agents
are INJECTED synthetically and the live refresh timer is frozen.

Proves:
  U1. _truncate leaves a short string untouched.
  U2. _truncate clips an over-width string and appends a trailing `…` (and the
      result fits the budget).
  U3. _ctx_columns spreads NAME+PROJECT so columns occupy <= ~80% of the width,
      NAME gets the larger share, and both honor their minimums on a narrow term.
  H1. (headless) The Context tab mounts a single ContextHeaderWidget whose
      rendered line carries the expected labels: PANE NAME PROJECT SESSION CTX%
      STATE.
  H2. (headless) The header content width equals a data row's content width, so
      the columns line up (same left inset).
  H3. (headless) A deliberately long NAME renders with a trailing `…`.

No transcript CONTENT is printed.
"""

from __future__ import annotations

import asyncio
import sys

from agents_tui import data
from agents_tui.app import (
    AgentsApp,
    ContextHeaderWidget,
    _ctx_columns,
    _truncate,
    _CTX_NAME_MIN,
    _CTX_PROJECT_MIN,
)
from agents_tui.data import Agent


LONG_NAME = "a-really-long-agent-name-that-overflows"


def make_agents() -> list[Agent]:
    """One synthetic agent whose NAME is deliberately long enough to overflow a
    narrow column, plus a real cwd so PROJECT resolves to a basename."""
    return [
        Agent(session="cc-long-1", session_id="fa77fefddeadbeef",
              active_pane="%3", project="lexi-backend", task="build",
              state="idle", pane_title=LONG_NAME,
              cwd="/Users/kiran/Desktop/lexi-backend"),
    ]


def run_unit(check) -> None:
    # ---- U1: short string untouched ----
    s = _truncate("short", 18)
    check("U1.truncate_short_untouched", s == "short", f"({s!r})")

    # ---- U2: over-width string clipped + ellipsis, fits budget ----
    t = _truncate(LONG_NAME, 18)
    ok = t.endswith("…") and len(t) == 18 and t != LONG_NAME
    check("U2.truncate_overflow_ellipsis", ok, f"({t!r} len={len(t)})")

    # ---- U3: column spread <= ~80%, NAME-heavy, minimums honored ----
    name_w, project_w = _ctx_columns(152)
    fixed = 6 + 8 + 5 + 10 + 5  # PANE+SESSION+CTX%+STATE + 5 gaps
    total = fixed + name_w + project_w
    spread_ok = total <= int(152 * 0.80) + 1  # +1 slack for int rounding
    name_heavy = name_w > project_w
    # narrow terminal must not collapse below the minimums
    nmin, pmin = _ctx_columns(20)
    mins_ok = nmin >= _CTX_NAME_MIN and pmin >= _CTX_PROJECT_MIN
    check("U3.columns_spread_name_heavy_min",
          spread_ok and name_heavy and mins_ok,
          f"(name={name_w} proj={project_w} total={total} "
          f"cap={int(152 * 0.80)} narrow=({nmin},{pmin}))")


async def run_headless(check) -> None:
    data.load_monitor_states_by_sid = lambda: {}
    app = AgentsApp()
    async with app.run_test(size=(160, 40)) as pilot:
        await pilot.pause()
        app.refresh_data = lambda: None  # freeze live refresh
        app._apply_agents(make_agents())
        await pilot.pause()
        await pilot.press("tab")  # -> Context tab
        await pilot.pause()

        pane = app.query_one("#context")

        # ---- H1: exactly one header widget with the expected labels ----
        headers = list(pane.query(ContextHeaderWidget))
        hdr_text = headers[0].render().plain if headers else ""
        labels = ["PANE", "NAME", "PROJECT", "SESSION", "CTX%", "STATE"]
        labels_ok = (len(headers) == 1
                     and all(lbl in hdr_text for lbl in labels))
        print(f"    H1 header line: |{hdr_text}|")
        check("H1.header_row_with_labels", labels_ok,
              f"(n={len(headers)} text={hdr_text!r})")

        # ---- H2: header content width == row content width (columns align) ----
        rows = list(pane._row_widgets.values())
        row = rows[0] if rows else None
        align_ok = (headers and row is not None
                    and headers[0].size.width == row.size.width)
        print(f"    H2 hdr_w={headers[0].size.width if headers else None} "
              f"row_w={row.size.width if row is not None else None}")
        check("H2.header_aligns_with_rows", bool(align_ok),
              f"(hdr={headers[0].size.width if headers else None} "
              f"row={row.size.width if row is not None else None})")

        # ---- H3: a long NAME renders with a trailing ellipsis ----
        # Force a narrow render so the long name MUST overflow its column.
        narrow = AgentsApp()
        async with narrow.run_test(size=(80, 40)) as p2:
            await p2.pause()
            narrow.refresh_data = lambda: None
            narrow._apply_agents(make_agents())
            await p2.pause()
            await p2.press("tab")
            await p2.pause()
            npane = narrow.query_one("#context")
            nrow = list(npane._row_widgets.values())[0]
            line = nrow.render().plain
            has_ellipsis = "…" in line
            print(f"    H3 narrow row: |{line.rstrip()}|")
            check("H3.long_name_ellipsis", has_ellipsis,
                  f"(ellipsis={has_ellipsis})")


async def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    orig_load = data.load_monitor_states_by_sid
    print("===== CTX COLUMNS TESTS =====")
    try:
        run_unit(check)
        await run_headless(check)
    finally:
        data.load_monitor_states_by_sid = orig_load

    print("\n===== RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
