"""test_layer4.py — proofs for the FOUR layered agents-tui changes.

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_layer4.py

Proves (without printing transcript content — only session names, ids, counts,
and rendered card text which is just session titles):

  CHANGE 1 — scroll stability:
    1a. _scroll_to_selected() resolves the PERSISTENT keyed selected row and
        calls scroll_visible on it (spied) after a `down` press, without error.
    1b. A steady-state refresh (same order) does NOT change the agentlist
        scroll_offset (captured before/after a same-snapshot re-apply).
    1c. PreviewPane auto-scrolls to end ONLY when the selected agent CHANGES,
        not on a same-agent refresh (tracked via _shown_key).

  CHANGE 2 — pane_title as card name:
    2a. clean_pane_title strips the spinner glyph on the documented inputs.
    2b. An Agent WITH a pane_title renders the cleaned pane_title as the
        title line and `project · task` as a SEPARATE dim subtitle (3 lines,
        .tagged height). Precedence pane_title > label verified.
    2c. _matches() filters on pane_title, not just label.

  CHANGE 3 — row cluster = effort + ctx only, ctx un-gated:
    3a. NO live row's rendered text contains "5h" (removed from rows).
    3b. ctx renders for ~all sessions that have ctx (count printed).
    3c. ctx is NOT gated on effort: a ctx-but-no-effort Agent still shows
        "ctx NN%" (constructed + at least one live example).
    3d. The preview meta block STILL contains 5h and 7d (not removed there).

  CHANGE 4 — wid resolution stamp -> title-match -> none/ambiguous:
    4a. clean-1:1 title match resolves to a real aerospace wid (constructed from
        the live Ghostty window list so it's deterministic).
    4b. ambiguous title (2+ windows) returns AMBIGUOUS_WID, never a wrong wid.
    4c. no-match title returns None.
    4d. stamped @aerospace_wid takes precedence over title-match.
    4e. real coverage breakdown of current sessions: stamp / title-match /
        neither (names + ids only).
"""

from __future__ import annotations

import asyncio
import sys

from agents_tui import data
from agents_tui.app import AgentRow, AgentsApp, PreviewPane
from agents_tui.data import Agent


def row_text(row: AgentRow) -> str:
    r = row.render()
    return r.plain if hasattr(r, "plain") else str(r)


async def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    # =====================================================================
    # CHANGE 2a — clean_pane_title unit (glyph strip)
    # =====================================================================
    print("===== CHANGE 2a: clean_pane_title glyph strip =====")
    ct_cases = [
        ("✳ template-fill-overhaul", "template-fill-overhaul"),
        ("⠂ tf-gp-handoff", "tf-gp-handoff"),
        ("Claude Code", "Claude Code"),
        ("Fix thinking rail alignment and spacing",
         "Fix thinking rail alignment and spacing"),
        ("✳ Check WA MCP server access", "Check WA MCP server access"),
        ("", ""),
        (None, ""),
    ]
    ct_ok = True
    for raw, want in ct_cases:
        got = data.clean_pane_title(raw)
        ok = got == want
        ct_ok = ct_ok and ok
        print(f"    {'OK ' if ok else 'BAD'} {raw!r:48} -> {got!r}")
    check("2a.clean_pane_title_strips_glyph", ct_ok)

    # =====================================================================
    # CHANGE 4 — wid resolution (no focus calls anywhere)
    # =====================================================================
    print("\n===== CHANGE 4: wid resolution =====")
    wins = data.aerospace_windows()
    aero_wids = {w for w, a, t in wins}
    ghostty = [(w, a, t) for w, a, t in wins if a == "Ghostty"]

    # build a multiplicity map of cleaned Ghostty titles
    from collections import Counter
    cleaned = [data.clean_pane_title(t).strip().lower() for _, a, t in ghostty]
    mult = Counter(c for c in cleaned if c)

    # 4a — find a cleaned title with multiplicity exactly 1; build an agent whose
    #      pane_title is that title and assert it resolves to that single wid.
    uniq_title = None
    uniq_wid = None
    for w, a, t in ghostty:
        c = data.clean_pane_title(t).strip().lower()
        if c and mult[c] == 1:
            uniq_title = data.clean_pane_title(t)
            uniq_wid = w
            break
    if uniq_title is not None:
        ag = Agent(session="probe-unique", pane_title=uniq_title)
        got = data.resolve_wid(ag, wins)
        print(f"    unique-title agent pane_title={uniq_title!r} "
              f"-> wid={got} (expected {uniq_wid}, in_aero={got in aero_wids})")
        check("4a.unique_title_resolves_to_real_wid",
              got == uniq_wid and got in aero_wids,
              f"(wid={got})")
    else:
        check("4a.unique_title_resolves_to_real_wid", False,
              "(no unique Ghostty title available in aerospace)")

    # 4b — ambiguous title (multiplicity >= 2) -> AMBIGUOUS_WID
    ambig_title = None
    for c, n in mult.items():
        if n >= 2:
            ambig_title = c
            break
    if ambig_title is not None:
        ag = Agent(session="probe-ambig", pane_title=ambig_title)
        got = data.resolve_wid(ag, wins)
        print(f"    ambiguous-title {ambig_title!r} (x{mult[ambig_title]}) "
              f"-> {got}")
        check("4b.ambiguous_title_returns_sentinel",
              got == data.AMBIGUOUS_WID, f"(got={got})")
    else:
        # no natural duplicate — synthesize one by duplicating a row
        synth = list(wins)
        if ghostty:
            w, a, t = ghostty[0]
            synth.append(("999999", "Ghostty", t))
            ag = Agent(session="probe-ambig",
                       pane_title=data.clean_pane_title(t))
            got = data.resolve_wid(ag, synth)
            print(f"    (synthesized dup) {t!r} -> {got}")
            check("4b.ambiguous_title_returns_sentinel",
                  got == data.AMBIGUOUS_WID, f"(got={got})")
        else:
            check("4b.ambiguous_title_returns_sentinel", False,
                  "(no Ghostty windows to test)")

    # 4c — no-match title -> None
    ag = Agent(session="probe-none",
               pane_title="zzz-no-such-window-title-xyzzy")
    got = data.resolve_wid(ag, wins)
    check("4c.no_match_returns_none", got is None, f"(got={got})")

    # 4d — stamped wid wins over title-match
    ag = Agent(session="probe-stamp", aerospace_wid="424242",
               pane_title=(uniq_title or "anything"))
    got = data.resolve_wid(ag, wins)
    check("4d.stamp_takes_precedence", got == "424242", f"(got={got})")

    # 4e — real coverage breakdown
    agents = data.gather_agents()
    n_stamp = n_title = n_ambig = n_none = 0
    title_examples: list[str] = []
    for a in agents:
        if a.aerospace_wid:
            n_stamp += 1
            continue
        w = data.resolve_wid(a, wins)
        if w == data.AMBIGUOUS_WID:
            n_ambig += 1
        elif w:
            n_title += 1
            title_examples.append(f"{a.session}->{w}")
        else:
            n_none += 1
    print(f"\n    coverage over {len(agents)} live sessions: "
          f"stamp={n_stamp} title-match={n_title} "
          f"ambiguous={n_ambig} none={n_none}")
    if title_examples:
        print("    title-match resolved:", ", ".join(title_examples))
    # we only assert the breakdown SUMS correctly and resolution never errored
    check("4e.coverage_breakdown_consistent",
          n_stamp + n_title + n_ambig + n_none == len(agents),
          f"(sum={n_stamp + n_title + n_ambig + n_none}/{len(agents)})")

    # =====================================================================
    # CHANGE 3 — row cluster effort+ctx only, ctx un-gated (constructed)
    # =====================================================================
    print("\n===== CHANGE 3: row cluster (constructed) =====")
    # ctx-but-no-effort -> must show ctx
    a_ctx_only = Agent(session="c1", project="p", task="t",
                       pct=12, effort=None, snippet="x")
    a_both = Agent(session="c2", project="p", task="t",
                   pct=44, effort="xhigh", snippet="x", five_h_pct=50.0)
    a_effort_only = Agent(session="c3", project="p", task="t",
                          pct=None, effort="max", snippet="x")
    r_ctx = AgentRow(a_ctx_only, False)
    r_both = AgentRow(a_both, False)
    r_eff = AgentRow(a_effort_only, False)
    # CHANGE B: the ctx cluster lives on the LAST line (snippet + ctx ONLY);
    # the effort token now lives on LINE 1 (next to the age). Line 1 holds the
    # age too, which can legitimately read "5h"/"7d" — so only inspect the
    # cluster (last) line for the 5h/7d check.
    cl_ctx = row_text(r_ctx).split(chr(10))[-1]
    cl_both = row_text(r_both).split(chr(10))[-1]
    cl_eff = row_text(r_eff).split(chr(10))[-1]
    l1_both = row_text(r_both).split(chr(10))[0]
    l1_eff = row_text(r_eff).split(chr(10))[0]
    print("    ctx-only  cluster:", repr(cl_ctx.strip()))
    print("    both      cluster:", repr(cl_both.strip()))
    print("    effort-only      :", repr(cl_eff.strip()))
    print("    both     line1   :", repr(l1_both.strip()))
    print("    effort   line1   :", repr(l1_eff.strip()))
    check("3c.ctx_shows_without_effort",
          "ctx 12%" in cl_ctx and "ctx 44%" in cl_both,
          "(ctx renders un-gated on last line)")
    # CHANGE B: effort moved UP to line 1; it must NOT be on the cluster (last)
    # line and MUST be on line 1 when present.
    check("3c3.effort_on_line1_not_cluster",
          "xhigh" in l1_both and "xhigh" not in cl_both
          and "max" in l1_eff and "max" not in cl_eff,
          "(effort on line1, absent from cluster line)")
    # "5h"/"7d" must not appear in the CLUSTER (last) line of any row
    check("3a.constructed_rows_have_no_5h",
          "5h" not in cl_ctx and "5h" not in cl_both and "5h" not in cl_eff
          and "7d" not in cl_ctx and "7d" not in cl_both and "7d" not in cl_eff,
          "(no 5h/7d token in row cluster)")

    # =====================================================================
    # boot the app for live row + preview + scroll proofs
    # =====================================================================
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
        else:
            # --- CHANGE 3a/3b: no live row has 5h; ctx count ---
            # Inspect the CLUSTER line (last line) only — the age on line 1 can
            # legitimately read "5h"/"7d" and must not be mistaken for a token.
            rows = list(app.query(AgentRow))

            def cluster_line(r) -> str:
                return row_text(r).split(chr(10))[-1]

            any_5h = any("5h" in cluster_line(r) or "7d" in cluster_line(r)
                         for r in rows)
            ctx_rows = sum(1 for r in rows
                           if "ctx " in cluster_line(r)
                           and "ctx —" not in cluster_line(r))
            # rows showing ctx WITHOUT a preceding effort token (un-gated proof)
            ctx_no_effort = 0
            for r in rows:
                a = r.agent
                if a and a.pct is not None and not a.effort:
                    if "ctx " in cluster_line(r):
                        ctx_no_effort += 1
            # CHANGE B: for live rows WITH an effort, the effort token must be
            # on LINE 1 (next to the age) and NOT on the cluster (last) line.
            def first_line(r) -> str:
                return row_text(r).split(chr(10))[0]

            eff_rows = 0
            eff_on_line1_ok = True
            for r in rows:
                a = r.agent
                if a and a.effort:
                    eff_rows += 1
                    if a.effort not in first_line(r) \
                            or a.effort in cluster_line(r):
                        eff_on_line1_ok = False
            print("\n===== CHANGE 3: live rows =====")
            print(f"    any cluster contains '5h'/'7d' : {any_5h}")
            print(f"    rows showing real 'ctx NN%'    : {ctx_rows}")
            print(f"    rows w/ctx but NO effort       : {ctx_no_effort}")
            print(f"    rows w/effort (token on line1) : {eff_rows} "
                  f"(all_on_line1={eff_on_line1_ok})")
            check("3a.no_live_row_has_5h", not any_5h, "(5h removed from rows)")
            check("3b.ctx_renders_for_most_sessions",
                  ctx_rows >= 10, f"(ctx_rows={ctx_rows})")
            check("3c2.live_ctx_without_effort_exists",
                  ctx_no_effort >= 1, f"(count={ctx_no_effort})")
            check("3c4.live_effort_on_line1_not_cluster",
                  eff_on_line1_ok,
                  f"(eff_rows={eff_rows}, all_on_line1={eff_on_line1_ok})")

            # --- CHANGE 3d: preview meta STILL has 5h and 7d ---
            sel = app.selected_agent
            meta_txt = ""
            if sel is not None:
                meta = PreviewPane._meta_cluster(sel)
                meta_txt = meta.plain if hasattr(meta, "plain") else str(meta)
            print(f"    preview meta has 5h/7d       : "
                  f"{'5h' in meta_txt and '7d' in meta_txt}")
            check("3d.preview_meta_keeps_5h_7d",
                  "5h" in meta_txt and "7d" in meta_txt,
                  "(5h/7d still in preview)")

            # --- CHANGE 2b: live card shows cleaned pane_title as title ---
            print("\n===== CHANGE 2b: live card titles (pane_title leads) =====")
            shown = 0
            ptitle_as_title_ok = True
            for r in rows:
                a = r.agent
                if a and a.pane_title:
                    lines = row_text(r).split("\n")
                    title_line = lines[0]
                    sub_line = lines[1] if len(lines) > 1 else ""
                    title_has_pt = a.pane_title in title_line
                    sub_has_label = a.label in sub_line
                    three_lines = len(lines) == 3
                    if shown < 3:
                        print(f"    title={a.pane_title!r:40} "
                              f"sub_has_label={sub_has_label} "
                              f"lines={len(lines)}")
                    if not (title_has_pt and sub_has_label and three_lines):
                        ptitle_as_title_ok = False
                    shown += 1
            check("2b.live_pane_title_is_card_title",
                  shown >= 1 and ptitle_as_title_ok,
                  f"(examples={shown})")

            # --- CHANGE 2c: filter on pane_title ---
            # find a session with a distinctive pane_title token
            target = None
            for a in app.agents:
                if a.pane_title and a.pane_title not in a.label:
                    # pick a token unlikely to be in the label
                    tok = a.pane_title.split()[0].split("-")[0]
                    if len(tok) >= 4 and tok.lower() not in a.label.lower():
                        target = (a, tok)
                        break
            if target:
                a, tok = target
                app.filter_text = tok
                matched = app._matches(list(app.agents))
                hit = any(m.session == a.session for m in matched)
                app.filter_text = ""
                print(f"    filter token={tok!r} from pane_title "
                      f"matched session={hit}")
                check("2c.filter_uses_pane_title", hit,
                      f"(token={tok!r})")
            else:
                # fallback: synthesize
                app.filter_text = "xyzzysent"
                synth_agent = Agent(session="syn", project="p", task="t",
                                    pane_title="xyzzysentinel-title")
                matched = app._matches([synth_agent])
                app.filter_text = ""
                check("2c.filter_uses_pane_title",
                      len(matched) == 1,
                      "(synthesized pane_title match)")

            # --- CHANGE 1a / Change C: the DEFERRED scroll re-queries the
            #     PERSISTENT keyed row and scrolls the #agentlist container to
            #     it. We spy the container's scroll_to_widget and confirm it is
            #     invoked (after the scheduled refresh) with the SAME persistent
            #     widget object held in app._rows for the selected key.
            print("\n===== CHANGE 1: scroll stability =====")
            from textual.containers import VerticalScroll as _VS
            app._selected_key = app._key(app._filtered[0])
            before_key = app._selected_key
            await pilot.press("down")
            await pilot.pause()
            sel_key = app._selected_key
            sel_row = app._rows.get(sel_key)
            lv_spy = app.query_one("#agentlist", _VS)
            spied = {"called": 0, "widget_ok": False}
            orig_stw = lv_spy.scroll_to_widget

            def _spy_stw(widget, *args, **kwargs):
                spied["called"] += 1
                spied["widget_ok"] = widget is app._rows.get(sel_key)
                return orig_stw(widget, *args, **kwargs)

            lv_spy.scroll_to_widget = _spy_stw  # type: ignore[assignment]
            app._scroll_to_selected()
            # the scroll is deferred via call_after_refresh -> pump the loop
            await pilot.pause()
            await pilot.pause()
            print(f"    selected changed {before_key != sel_key}; "
                  f"deferred scroll hit container scroll_to_widget "
                  f"x{spied['called']} with persistent row "
                  f"{spied['widget_ok']}")
            check("1a.scroll_to_selected_targets_persistent_row",
                  spied["called"] >= 1 and spied["widget_ok"]
                  and sel_row is app._rows.get(sel_key),
                  f"(calls={spied['called']}, widget_ok={spied['widget_ok']})")
            lv_spy.scroll_to_widget = orig_stw  # type: ignore[assignment]

            # --- CHANGE 1b: steady-state refresh keeps scroll_offset ---
            from textual.containers import VerticalScroll
            lv = app.query_one("#agentlist", VerticalScroll)
            base = list(app.agents)
            app._apply_agents(list(base))
            await pilot.pause()
            off_before = lv.scroll_offset
            app._apply_agents(list(base))  # same order -> no reorder
            await pilot.pause()
            off_after = lv.scroll_offset
            print(f"    scroll_offset before/after steady refresh: "
                  f"{tuple(off_before)} / {tuple(off_after)}")
            check("1b.steady_refresh_keeps_scroll_offset",
                  tuple(off_before) == tuple(off_after),
                  f"({tuple(off_before)} == {tuple(off_after)})")

            # --- CHANGE 1c: preview auto-scrolls only on agent change ---
            preview = app.query_one(PreviewPane)
            # select agent A
            app._selected_key = app._key(app._filtered[0])
            app._update_preview()
            await pilot.pause()
            key_a = preview._shown_key
            # same agent refresh -> _shown_key unchanged, no agent_changed
            app._update_preview()
            await pilot.pause()
            same_after = preview._shown_key
            # select agent B (different) -> _shown_key changes
            if len(app._filtered) > 1:
                app._selected_key = app._key(app._filtered[1])
                app._update_preview()
                await pilot.pause()
                key_b = preview._shown_key
                changed = key_b != key_a
            else:
                changed = True
                key_b = key_a
            print(f"    preview _shown_key A={str(key_a)[:12]} "
                  f"sameRefresh={str(same_after)[:12]} "
                  f"B={str(key_b)[:12]} changed={changed}")
            check("1c.preview_tracks_agent_change",
                  same_after == key_a and changed,
                  f"(same={same_after == key_a} changed={changed})")

    # =====================================================================
    # CHANGE C — GENUINE scroll-into-view proof (small vertical viewport).
    # Boot at size (140, 16) so only ~2 cards fit; press `down` PAST the fold
    # and assert (hard) that the selected persistent row's region stays FULLY
    # within the #agentlist viewport AND the scroll offset ADVANCES down; then
    # press `up` and assert the offset comes back DOWN with the row still
    # visible. This proves the actual fix (deferred scroll_to_widget), not just
    # that a scroll method was called.
    # =====================================================================
    print("\n===== CHANGE C: scroll-into-view (viewport=16) =====")
    from textual.containers import VerticalScroll as _VS2
    app2 = AgentsApp()
    async with app2.run_test(size=(140, 16)) as pilot2:
        await pilot2.pause()
        for _ in range(40):
            if app2.agents:
                break
            await asyncio.sleep(0.1)
        await pilot2.pause()
        await pilot2.pause()

        if len(app2._filtered) < 4:
            check("C.scroll_into_view", False,
                  f"(need >=4 agents to test scroll, got "
                  f"{len(app2._filtered)})")
        else:
            lv2 = app2.query_one("#agentlist", _VS2)
            vtop = lv2.region.y
            vbot = lv2.region.y + lv2.region.height
            n = len(app2._filtered)
            steps = min(n - 1, 8)
            print(f"    {n} agents, viewport screen-rows {vtop}..{vbot}")

            def row_in_view(key) -> tuple[bool, int, int]:
                row = app2._rows.get(key)
                reg = row.region
                return (reg.y >= vtop and reg.y + reg.height <= vbot,
                        reg.y, reg.y + reg.height)

            # press DOWN past the fold
            down_offs: list[int] = []
            all_in_view_down = True
            advanced_down = True
            prev = lv2.scroll_offset.y
            for s in range(steps):
                await pilot2.press("down")
                await pilot2.pause()
                await pilot2.pause()  # let the deferred scroll settle
                off = lv2.scroll_offset.y
                inv, rtop, rbot = row_in_view(app2._selected_key)
                down_offs.append(off)
                if not inv:
                    all_in_view_down = False
                # once we're past the first viewport-full, the offset must grow
                if s >= 2 and off <= prev:
                    advanced_down = False
                print(f"    down{s:2d} off.y={off:3d} row=({rtop},{rbot}) "
                      f"in_view={inv}")
                prev = off

            offset_grew = down_offs[-1] > down_offs[0]

            # press UP back toward the top
            up_offs: list[int] = []
            all_in_view_up = True
            for s in range(steps):
                await pilot2.press("up")
                await pilot2.pause()
                await pilot2.pause()
                off = lv2.scroll_offset.y
                inv, rtop, rbot = row_in_view(app2._selected_key)
                up_offs.append(off)
                if not inv:
                    all_in_view_up = False
                print(f"    up  {s:2d} off.y={off:3d} row=({rtop},{rbot}) "
                      f"in_view={inv}")

            offset_came_back = up_offs[-1] < up_offs[0]

            print(f"    down offsets: {down_offs}")
            print(f"    up   offsets: {up_offs}")
            print(f"    all_in_view down={all_in_view_down} up={all_in_view_up}"
                  f" | offset grew={offset_grew} came_back={offset_came_back}")

            # HARD assertion: visibility must hold both ways AND the offset must
            # advance down then come back up. (Hard-fail so it can't pass
            # silently.)
            ok = (all_in_view_down and all_in_view_up
                  and advanced_down and offset_grew and offset_came_back)
            check("C.scroll_into_view", ok,
                  f"(down_in_view={all_in_view_down} up_in_view={all_in_view_up}"
                  f" grew={offset_grew} back={offset_came_back})")
            if not ok:
                raise AssertionError(
                    "CHANGE C scroll-into-view FAILED: "
                    f"down_in_view={all_in_view_down} "
                    f"up_in_view={all_in_view_up} grew={offset_grew} "
                    f"came_back={offset_came_back} "
                    f"down_offs={down_offs} up_offs={up_offs}")

    # ----- report -----
    print("\n===== LAYER-4 TEST RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
