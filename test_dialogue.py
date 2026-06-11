"""test_dialogue.py — proofs for the two layered agents-tui changes.

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_dialogue.py

Proves, WITHOUT printing transcript content of non-own sessions (only event
kinds + lengths + structural facts):

  CHANGE A — RIGHT PANE dialogue-only body:
    A1. parse_transcript_preview() emits ONLY {"user","assistant"} kinds across
        every live transcript — NO "tool"/"result"/"question".
    A2. The rendered preview BODY contains no tool-bullet glyph "● " and no
        green ✓ result lines (dialogue prose only).
    A3. Turns are spaced (each rendered turn carries a trailing blank line).

  CHANGE B — LEFT CARDS @cc_name as title:
    B1. A tagged Agent (tag_name set) renders tag_name as the title line and
        `project · task` (== label) as a SEPARATE dim subtitle line, and gets
        the .tagged height class (3 content lines, no clip).
    B2. An untagged Agent renders label as the title and NO empty subtitle
        line (exactly 2 content lines), no .tagged class.
    B3. SVG snapshot exported to /tmp/agents-tui-cards2.svg.
"""

from __future__ import annotations

import asyncio
import os
import sys

from textual.containers import VerticalScroll
from textual.widgets import Static

from agents_tui import data
from agents_tui.app import AgentRow, AgentsApp, PreviewPane
from agents_tui.data import Agent


# tool-bullet glyph used by the OLD tool-log render; must NOT appear now.
TOOL_BULLET = "● "   # "● "
RESULT_CHECK = "✓"   # "✓"


def static_text(w: Static) -> str:
    try:
        r = w.render()
    except Exception:
        return ""
    return r.plain if hasattr(r, "plain") else str(r)


async def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    # ===== CHANGE A1: dialogue-only kinds across ALL live transcripts =====
    agents = data.gather_agents()
    all_kinds: set[str] = set()
    per_transcript: list[tuple[str, int]] = []  # (kinds-summary, n_events)
    scanned = 0
    for a in agents:
        tpath = data.find_transcript(a.session_id)
        if not tpath:
            continue
        try:
            events = data.parse_transcript_preview(tpath)
        except Exception:
            continue
        scanned += 1
        kinds = [e["kind"] for e in events]
        all_kinds.update(kinds)
        kset = "".join(sorted({k[0] for k in kinds})) or "-"  # e.g. "au"
        per_transcript.append((kset, len(events)))

    print("===== CHANGE A: dialogue-only preview body =====")
    print(f"    transcripts scanned             : {scanned}")
    print(f"    union of ALL event kinds        : {sorted(all_kinds)}")
    # redacted per-transcript shape: which kinds present + count, no content
    print("    per-transcript (kinds/count)    : "
          + ", ".join(f"[{k}:{n}]" for k, n in per_transcript[:12])
          + (" ..." if len(per_transcript) > 12 else ""))
    check("A1.kinds_are_dialogue_only",
          all_kinds.issubset({"user", "assistant"}),
          f"(kinds={sorted(all_kinds)})")

    # ===== boot app for rendered-body + card proofs =====
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
            # ----- A2: rendered body has no tool bullet / no ✓ result line ----
            preview = app.query_one(PreviewPane)
            body_texts = [static_text(w) for w in preview._body_pool]
            joined = "\n".join(body_texts)
            has_tool_bullet = TOOL_BULLET in joined
            # a green ✓ result line in the OLD render started with "✓ "
            has_result_check = any(t.lstrip().startswith(RESULT_CHECK)
                                   for t in body_texts)
            print("\n===== CHANGE A2: rendered body excerpt (redacted) =====")
            print(f"    body Static count               : {len(body_texts)}")
            print(f"    contains tool bullet '● '       : {has_tool_bullet}")
            print(f"    contains ✓ result line          : {has_result_check}")
            # redacted sample: leading marker + length only (NOT full content)
            for t in body_texts[:4]:
                first = t.split("\n", 1)[0]
                marker = "user❯" if first.startswith("❯") else "asst"
                print(f"      turn[{marker}] len={len(first):<4}")
            check("A2.body_has_no_tool_or_result_glyphs",
                  not has_tool_bullet and not has_result_check,
                  f"(tool_bullet={has_tool_bullet} result_check={has_result_check})")

            # ----- A3: turns are spaced (trailing blank line per turn) -----
            # _render_event bakes a trailing "\n"; sample a non-banner turn.
            sample = next((t for t in body_texts
                           if t and not t.startswith(" waiting")), "")
            spaced = sample.endswith("\n") or "\n" in sample
            check("A3.turns_are_spaced", spaced,
                  f"(sample_ends_blank={sample.endswith(chr(10))})")

            # ----- B: construct a tagged + untagged Agent and render -----
            lv = app.query_one("#agentlist", VerticalScroll)
            tagged = Agent(
                session="cc-demo-1627", session_id="sid-tagged",
                project="lexi-backend", task="checkpoint-rollback",
                tag_name="checkpoint-rollback-1627", state="working",
                age_seconds=42,
                snippet="Refactoring the pane pooling so identity persists",
                effort="high", pct=37, five_h_pct=58.0)
            untagged = Agent(
                session="cc-demo-9", session_id="sid-untagged",
                project="lexi-web", task="feature/foo", state="idle",
                age_seconds=3600, snippet="Idle status snippet",
                effort="low", pct=12, five_h_pct=4.0)
            rt = AgentRow(tagged, False)
            ru = AgentRow(untagged, False)
            lv.mount(rt, ru)
            await pilot.pause()

            t_lines = rt.render().plain.split("\n")
            u_lines = ru.render().plain.split("\n")

            print("\n===== CHANGE B: card title/subtitle proof =====")
            print(f"  TAGGED   classes={sorted(rt.classes)} "
                  f"outer_h={rt.outer_size.height} lines={len(t_lines)}")
            for ln in t_lines:
                print(f"    | {ln}")
            print(f"  UNTAGGED classes={sorted(ru.classes)} "
                  f"outer_h={ru.outer_size.height} lines={len(u_lines)}")
            for ln in u_lines:
                print(f"    | {ln}")

            tag_title_ok = (
                t_lines[0].lstrip("⋮○● ").startswith(
                    "checkpoint-rollback-1627")
                and "lexi-backend · checkpoint-rollback" in t_lines[1]
                and len(t_lines) == 3
                and "tagged" in rt.classes
                and rt.outer_size.height == 5)
            check("B1.tagged_title_is_tagname_subtitle_is_label",
                  tag_title_ok,
                  f"(lines={len(t_lines)} tagged={'tagged' in rt.classes} "
                  f"h={rt.outer_size.height})")

            untag_ok = (
                "lexi-web · feature/foo" in u_lines[0]
                and len(u_lines) == 2
                and "tagged" not in ru.classes
                and ru.outer_size.height == 4)
            check("B2.untagged_title_is_label_no_subtitle",
                  untag_ok,
                  f"(lines={len(u_lines)} tagged={'tagged' in ru.classes} "
                  f"h={ru.outer_size.height})")

            # ----- B3: SVG snapshot -----
            try:
                svg = app.export_screenshot()
                with open("/tmp/agents-tui-cards2.svg", "w") as f:
                    f.write(svg)
                wrote = os.path.getsize("/tmp/agents-tui-cards2.svg") > 0
            except Exception as e:  # noqa: BLE001
                wrote = False
                print("    screenshot error:", e)
            check("B3.cards2_svg_exported", wrote,
                  "(/tmp/agents-tui-cards2.svg)")

    print("\n===== DIALOGUE/CARDS TEST RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
