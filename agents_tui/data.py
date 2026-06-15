"""data.py — gather + derive agent state for the agents-tui dashboard.

All reads are from sources that already exist in the cockpit:
  - tmux        : session list, active pane, attention tint
                  (window-active-style bg=colour52), the aerospace wid stamp
                  (@aerospace_wid).
  - ctx registry: /tmp/claude-ctx/<session_id>.json  -> ctx% + cwd + ts.
  - transcripts : ~/.claude/projects/*/<session_id>.jsonl -> last message + age.

PERFORMANCE NOTES (see spec):
  - For the LIST we only tail-read each transcript (last ~64KB) and CACHE the
    parsed result keyed on (path, mtime) so unchanged transcripts are skipped.
  - We only DEEP-parse (last ~256KB) the SELECTED agent's transcript, on demand.
  - tmux is batched where possible: one list-sessions, one list-panes -a per
    refresh; per-pane show-options is unavoidable for styles/opts but is cheap.

NO third-party deps: stdlib only (subprocess, json, glob, os, time, datetime).
"""

from __future__ import annotations

import glob
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Constants / tunables
# ---------------------------------------------------------------------------

CTX_DIR = "/tmp/claude-ctx"
NEEDY_STYLE = "bg=colour52"  # set by ~/.claude/hooks/tmux-attention.sh

# ctx-monitor sidecar files in CTX_DIR (see ~/.claude/tools/ctx-monitor/
# ctx-monitor.py). monitor-state.json holds per-pane cycle STATE; monitor.lock
# is the single-instance flock with the holder PID stamped inside.
MONITOR_STATE_FILE = os.path.join(CTX_DIR, "monitor-state.json")
MONITOR_LOCK_FILE = os.path.join(CTX_DIR, "monitor.lock")

# Internal monitor STATE identifiers -> human-readable dashboard labels.
# Mirrors ctx-monitor.py's STATE_LABELS EXACTLY (its ARMED/SAVE_SENT/...).
# The raw identifiers are what monitor-state.json persists; we map for display.
MONITOR_STATE_LABELS = {
    "ARMED": "watching",
    "SAVE_SENT": "checkpoint requested",
    "COMPACT_SENT": "compacting",
    "REORIENT_SENT": "reorienting",
    "ERROR": "ERROR - needs attention",
}
# Default raw state for a session the monitor has no record of yet.
MONITOR_DEFAULT_STATE = "ARMED"

# aerospace binary (matches the path the pilot test uses; falls back to PATH).
AEROSPACE_BIN = "/opt/homebrew/bin/aerospace"

# Sentinel returned by the title-match fallback when a cleaned pane_title maps
# to 2+ Ghostty windows: we must NOT guess-focus a wrong window, so resolve_wid
# surfaces this and the Enter handler shows a clearer "ambiguous" message.
AMBIGUOUS_WID = "__ambiguous__"

# pane_title values that carry no useful identity (the tmux command itself, a
# bare launcher name, etc.) — treated as "no title" so they neither become a
# card title nor a title-match key.
_USELESS_TITLES = {
    "", "tmux", "/opt/homebrew/bin/tmux", "/usr/bin/tmux", "agents",
    "zsh", "bash", "-zsh", "fish", "claude",
}

# "working" if NOT needy AND the agent's last activity is within this many
# seconds; otherwise "idle". Tunable here on purpose — 10s matches how often a
# busy agent emits transcript/ctx updates while actively running a turn.
WORKING_WINDOW_SECONDS = 10

# A row counts as "working" while its raw pane title is changing (Claude
# animates a spinner glyph in the title while generating) and for a short
# grace afterward, so a single missed refresh tick doesn't drop the spinner.
TITLE_WORKING_GRACE_SECONDS = 4.0

# 30 min: idle-but-recent still counts as active
ACTIVE_RECENT_SECONDS = 1800

# How much of the tail of a transcript to read for the LIST snippet (cheap).
LIST_TAIL_BYTES = 2 * 1024 * 1024   # 2 MB
# How much for the SELECTED agent's preview (richer, still bounded).
PREVIEW_TAIL_BYTES = 8 * 1024 * 1024  # 8 MB
MAX_LINE_BYTES = 200 * 1024  # skip single records larger than this (attachment/image blobs — never rendered as dialogue)

PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/*/{sid}.jsonl")

# /color palette for spawned/restarted agent windows. NEVER "green" (reserved
# for the dispatcher session). Lives here (the primitives layer) because both
# spawn_claude_window and restart_agent pick from it; app.py re-exports it.
SPAWN_COLORS = ["red", "blue", "yellow", "purple", "orange", "pink", "cyan"]

# Section ordering for the list: needs-you (red) on top, then running, then
# inactive (idle) at the bottom. Drives the PRIMARY sort key in gather_agents.
# Unknown states sink to the inactive section.
_SECTION_RANK = {"needs-input": 0, "working": 1, "idle": 2}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    """One Claude Code agent (one tmux session)."""
    session: str                       # raw tmux session name
    session_id: Optional[str] = None   # claude uuid (from ctx join), may be None
    active_pane: Optional[str] = None  # "%NN"
    project: str = ""                  # cwd basename
    task: str = ""                     # git branch / prettified session name
    pid: str = ""                      # trailing digits of cc-<base>-<pid>, else ""
    cwd: str = ""                      # abs path
    pct: Optional[int] = None          # ctx % (shown regardless of age)
    state: str = "idle"                # "needs-input" | "working" | "idle"
    live_state: str = "idle"           # auto-classified state, ignoring any manual override
    pinned: bool = False               # True when a manual override is forcing `state`
    attached: bool = False             # True if a tmux client has an open window
    active: bool = False               # working/needy/attached/recently-used
    age_seconds: Optional[float] = None
    snippet: str = ""                  # one-line last-message/status snippet
    aerospace_wid: Optional[str] = None
    # cleaned Claude session title from the active pane (#{pane_title}), spinner
    # glyph stripped. Used as the card title and as the title-match fallback for
    # aerospace window resolution. None when empty or a useless/generic value.
    pane_title: Optional[str] = None
    panes: list[str] = field(default_factory=list)
    # per-session metadata from the statusline tap (often empty on old taps
    # or before the session's first API call — every field degrades to None).
    model: Optional[str] = None        # friendly model name, e.g. "Opus"
    effort: Optional[str] = None       # low/medium/high/xhigh/max
    worktree: Optional[str] = None     # worktree name, or None
    five_h_pct: Optional[float] = None  # 5h rolling usage-limit %, 0-100
    five_h_reset: Optional[int] = None  # epoch seconds
    seven_d_pct: Optional[float] = None  # 7d rolling usage-limit %, 0-100
    seven_d_reset: Optional[int] = None  # epoch seconds

    @property
    def label(self) -> str:
        """The `project · task` string used for display + filtering."""
        if self.project and self.task:
            return f"{self.project} · {self.task}"
        return self.project or self.task or self.session

    @property
    def age_str(self) -> str:
        return humanize_age(self.age_seconds)

    @property
    def five_h_resets_in(self) -> str:
        """'resets in' string for the 5h window, '' if no/elapsed reset."""
        return humanize_resets_in(self.five_h_reset)

    @property
    def seven_d_resets_in(self) -> str:
        """'resets in' string for the 7d window, '' if no/elapsed reset."""
        return humanize_resets_in(self.seven_d_reset)


@dataclass
class ContextRow:
    """One row of the Context-tab dashboard (mirrors ctx-monitor's table).

    Columns map 1:1 to the monitor's render_lines(): serial #, PANE, NAME, DIR
    (repo dir basename), SESSION (first 8 chars of session id), CONTEXT (used %),
    STATE. `state_label` is the human-readable wording; `is_error` flags the row
    for RED rendering (mirrors the monitor's ERROR row tint).
    """
    pane: str                  # tmux pane id, e.g. "%0" ("-" if unknown)
    name: str                  # the cockpit's resolved NAME (better than @cc_name)
    dir: str                   # repo dir basename
    session8: str              # first 8 chars of session id ("-" if unknown)
    pct: Optional[int]         # ctx used %
    state_label: str           # human-readable STATE
    is_error: bool             # True -> render the row RED
    sid: Optional[str] = None  # raw Claude session id (for the reset-flag join)


@dataclass
class MonitorLiveness:
    """Liveness of the ctx-monitor sidecar (from monitor.lock's stamped PID)."""
    alive: bool
    pid: Optional[int]


# ---------------------------------------------------------------------------
# tmux helpers (thin subprocess wrappers; verbatim invocations from classic)
# ---------------------------------------------------------------------------

def _run(args: list[str], timeout: float = 4.0) -> str:
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
        return out.stdout
    except Exception:
        return ""


def list_sessions() -> list[str]:
    """`tmux list-sessions -F '#{session_name}'`, sorted (matches classic)."""
    out = _run(["tmux", "list-sessions", "-F", "#{session_name}"])
    return sorted(s for s in out.splitlines() if s.strip())


def sessions_attached() -> dict[str, bool]:
    """session_name -> True if a client is attached (has an open window)."""
    out = _run(["tmux", "list-sessions", "-F", "#{session_name}\t#{session_attached}"])
    d = {}
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) == 2:
            d[parts[0]] = parts[1].strip() not in ("", "0")
    return d


def list_panes_all() -> list[tuple[str, str]]:
    """`tmux list-panes -a -F '#{pane_id} #{session_name}'` -> [(pane, sess)]."""
    out = _run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{session_name}"])
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    return rows


def active_pane(session: str) -> Optional[str]:
    """Active pane id (pane_active == 1) for a session, else None."""
    out = _run(["tmux", "list-panes", "-t", session,
                "-F", "#{pane_id} #{pane_active}"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == "1":
            return parts[0]
    return None


def pane_opt(pane: str, opt: str) -> str:
    """`tmux show-options -p -t <pane> -qv <opt>` — value only, '' if unset."""
    if not pane:
        return ""
    return _run(["tmux", "show-options", "-p", "-t", pane, "-qv", opt]).strip()


def pane_title(pane: str) -> str:
    """Raw `#{pane_title}` for a pane (Claude's session title), '' if unset.

    For a live Claude session this is the spinner-prefixed session name, e.g.
    `✳ thinking-overflow-ui` or `⠂ tf-gp-handoff`. Caller should clean it with
    clean_pane_title() before display/matching.
    """
    if not pane:
        return ""
    return _run(["tmux", "display", "-p", "-t", pane, "#{pane_title}"]).strip()


# A leading "spinner glyph + space" prefix: one or more leading non-alphanumeric
# characters (the Claude spinner: ✳ ⠂ ⠐ ⠠ ⠄ · • etc.) that are NOT one of the
# title-opening chars we want to keep ( ( [ / ), followed by whitespace. We only
# strip when whitespace follows so we never eat into a title that legitimately
# starts with punctuation but has no glyph+space prefix.
_SPINNER_RE = re.compile(r"^[^\w\s(\[/]+\s+")


def clean_pane_title(raw: Optional[str]) -> str:
    """Strip the leading Claude spinner glyph (+ following whitespace) from a
    pane title and trim.

    Examples:
      "✳ template-fill-overhaul" -> "template-fill-overhaul"
      "⠂ tf-gp-handoff"          -> "tf-gp-handoff"
      "Claude Code"              -> "Claude Code"   (no glyph prefix)
      "Fix thinking rail …"      -> "Fix thinking rail …"
      ""                         -> ""

    Robust to plain-sentence titles (no glyph) and to titles that legitimately
    open with ( [ or / (kept as-is). Only a leading run of glyph-ish chars that
    is FOLLOWED BY whitespace is removed, so a title is never truncated mid-word.
    """
    if not raw:
        return ""
    s = raw.strip()
    # strip a leading "glyph(s) + whitespace" prefix if present
    m = _SPINNER_RE.match(s)
    if m:
        s = s[m.end():]
    return s.strip()


def _window_active_style(pane: str) -> str:
    """Value of window-active-style for a pane.

    We use the -qv form (value only). Be robust: if a tmux build prefixes the
    option name (`window-active-style bg=colour52`), strip that leading token.
    Returns one of: '', 'default', or a string containing 'bg=colour52'.
    """
    if not pane:
        return ""
    v = _run(["tmux", "show-options", "-p", "-t", pane, "-qv",
              "window-active-style"]).strip()
    if v.startswith("window-active-style "):
        v = v[len("window-active-style "):].strip()
    return v


def session_needy(panes: list[str]) -> bool:
    """True if ANY pane of the session has window-active-style bg=colour52."""
    for p in panes:
        if NEEDY_STYLE in _window_active_style(p):
            return True
    return False


# ---------------------------------------------------------------------------
# ctx registry  (pane -> session_id / pct / cwd / ts)
# ---------------------------------------------------------------------------

def load_ctx_by_pane() -> dict[str, dict]:
    """Read /tmp/claude-ctx/*.json -> { pane: {session_id, pct, cwd, ts} }.

    EXCLUDES monitor-state*.json / monitor.log / non-json. ctx% is surfaced
    regardless of age (no freshness gate — per spec).
    """
    out: dict[str, dict] = {}
    try:
        files = os.listdir(CTX_DIR)
    except OSError:
        return out
    for fn in files:
        if not fn.endswith(".json"):
            continue
        if fn.startswith("monitor-state"):
            continue
        try:
            with open(os.path.join(CTX_DIR, fn)) as f:
                j = json.load(f)
        except Exception:
            continue
        pane = j.get("pane")
        if not pane:
            continue
        out[pane] = _ctx_record(j)
    return out


def _ctx_record(j: dict) -> dict:
    """Normalize one ctx file's JSON into the per-agent ctx fields.

    Shared by the (legacy) pane keying and the session-based join so both
    surface the same fields. Old taps lack the extended keys entirely, so
    every one degrades gracefully (None / unparseable -> None).
    """
    return {
        "session_id": j.get("session_id"),
        "pct": j.get("pct"),
        "cwd": j.get("cwd", ""),
        "ts": j.get("ts", 0),
        "model": _clean_opt_str(j.get("model")),
        "effort": _clean_opt_str(j.get("effort")),
        "worktree": _clean_opt_str(j.get("worktree")),
        "five_h_pct": _parse_opt_float(j.get("five_h_pct")),
        "five_h_reset": _parse_opt_int(j.get("five_h_reset")),
        "seven_d_pct": _parse_opt_float(j.get("seven_d_pct")),
        "seven_d_reset": _parse_opt_int(j.get("seven_d_reset")),
    }


def _ctx_ts(rec: dict) -> float:
    """Freshness key for a ctx record (largest ts wins). Bad -> 0."""
    try:
        return float(rec.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def load_ctx_by_session(panes_all: list[tuple[str, str]]) -> dict[str, dict]:
    """Read /tmp/claude-ctx/*.json and attribute each to its LIVE tmux session.

    The robust join (see ISSUE 3): a ctx file stores a `pane`, but that pane is
    often stale / non-active and won't be the one we'd guess by walking a
    session's panes. So instead we resolve each ctx file's stored pane to the
    session that CURRENTLY owns it via the live `tmux list-panes -a` map, and
    attribute the file's fields to that session name. This works regardless of
    which pane is active or how many panes a session has.

    Edge cases:
      - A ctx file whose stored pane no longer exists in the live map (pane
        closed) does not resolve to any session and is dropped — correct, the
        session is likely gone too.
      - If MULTIPLE ctx files map to the same session (multiple panes each with
        a tap), the one with the freshest `ts` wins (robust against stale dupes).

    Returns { session_name: ctx_dict }.
    """
    pane_to_session = {pane: sess for pane, sess in panes_all}
    out: dict[str, dict] = {}
    try:
        files = os.listdir(CTX_DIR)
    except OSError:
        return out
    for fn in files:
        if not fn.endswith(".json"):
            continue
        if fn.startswith("monitor-state"):
            continue
        try:
            with open(os.path.join(CTX_DIR, fn)) as f:
                j = json.load(f)
        except Exception:
            continue
        pane = j.get("pane")
        if not pane:
            continue
        sess = pane_to_session.get(pane)
        if not sess:
            continue  # stale pane -> no live session owns it; drop
        rec = _ctx_record(j)
        prev = out.get(sess)
        if prev is None or _ctx_ts(rec) >= _ctx_ts(prev):
            out[sess] = rec  # freshest ts wins
    return out


# ---------------------------------------------------------------------------
# Process-truth ctx join (see MISATTRIBUTION fix)
#
# The pane-based join above (load_ctx_by_session) is unreliable: a ctx file's
# self-reported `pane` can be stale (recycled pane ids) or foreign (a Claude
# launched with an inherited TMUX_PANE records a pane it isn't really in), so a
# stale/foreign tap can collide on a pane and, being freshest, override the
# legit one — misattributing ctx%/effort/snippet to the wrong card.
#
# The authoritative source is ~/.claude/sessions/<pid>.json, a per-process
# record carrying {pid, sessionId, name, cwd, status}. We resolve each tmux
# session's REAL Claude sessionId by process truth: find the sessions-store
# record whose pid is the session's active-pane shell pid OR a descendant of it
# (via the `ps` process tree), then fetch ctx by that sessionId. The old
# pane-based join is retained only as a fallback when this can't resolve.
# ---------------------------------------------------------------------------

def _ps_parent_map() -> dict[str, str]:
    """pid -> ppid for all live processes (one `ps` call)."""
    out = _run(["ps", "-eo", "pid,ppid"])
    parent = {}
    for ln in out.splitlines()[1:]:
        p = ln.split()
        if len(p) == 2:
            parent[p[0]] = p[1]
    return parent


def _ancestors(pid: str, parent: dict[str, str]) -> list[str]:
    """Ancestor pids of `pid`, nearest first (bounded; cycle-safe)."""
    seen = []
    cur = pid
    for _ in range(40):
        cur = parent.get(cur)
        if not cur or cur in seen:
            break
        seen.append(cur)
    return seen


def _pane_pid_map() -> dict[str, str]:
    """tmux pane_id -> pane shell pid (one `tmux list-panes -a` call)."""
    out = _run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"])
    m = {}
    for ln in out.splitlines():
        p = ln.split()
        if len(p) == 2:
            m[p[0]] = p[1]
    return m


def _load_sessions_store() -> dict[str, dict]:
    """pid -> session record from ~/.claude/sessions/<pid>.json
    (records carry sessionId, name, cwd, status)."""
    store = {}
    for f in glob.glob(os.path.expanduser("~/.claude/sessions/*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if not d.get("sessionId"):
            continue
        pid = str(d.get("pid") or os.path.splitext(os.path.basename(f))[0])
        store[pid] = d
    return store


def load_ctx_by_sid() -> dict[str, dict]:
    """Index ctx taps by their OWN session_id (freshest-ts-wins per sid).
    Keyed by sessionId, NOT by the unreliable `pane` field."""
    out = {}
    try:
        files = os.listdir(CTX_DIR)
    except OSError:
        return out
    for fn in files:
        if not fn.endswith(".json") or fn.startswith("monitor-state"):
            continue
        try:
            with open(os.path.join(CTX_DIR, fn)) as f:
                j = json.load(f)
        except Exception:
            continue
        sid = j.get("session_id")
        if not sid:
            continue
        rec = _ctx_record(j)
        prev = out.get(sid)
        if prev is None or _ctx_ts(rec) >= _ctx_ts(prev):
            out[sid] = rec
    return out


def resolve_sid_for_pane(active_pane_id, pane_title_clean, pane_pids, store, parent):
    """The Claude sessionId actually running in `active_pane_id`, via process
    truth (sessions store pid ∩ the pane's process subtree). None if unresolved.
    When a pane hosts subagents too, the tiebreak order is: a name==pane_title
    match FIRST (the pane title is the authoritative identity), then
    status=='active', then the SHALLOWEST process in the subtree (the interactive
    session is the shallowest claude under the shell)."""
    if not active_pane_id:
        return None
    pane_pid = pane_pids.get(active_pane_id)
    if not pane_pid:
        return None
    cands = []
    for spid, d in store.items():
        if spid == pane_pid or pane_pid in _ancestors(spid, parent):
            cands.append((spid, d))
    if not cands:
        return None
    def depth(spid):
        anc = _ancestors(spid, parent)
        return anc.index(pane_pid) if pane_pid in anc else 0
    def rank(item):
        spid, d = item
        name = d.get("name") or ""
        return (0 if (name and name == pane_title_clean) else 1,
                d.get("status") != "active", depth(spid))
    cands.sort(key=rank)
    return cands[0][1].get("sessionId")


# ---------------------------------------------------------------------------
# Transcript tail-reading  (cached on (path, mtime))
# ---------------------------------------------------------------------------

# cache: path -> (mtime, parsed_result)
_list_cache: dict[str, tuple[float, dict]] = {}

# session -> last raw pane title / ts of last raw-title change. These drive the
# title-animation "working" detection (Claude animates a spinner glyph in the
# pane title while generating).
_prev_raw_title: dict[str, str] = {}        # session -> last raw pane title
_last_title_change: dict[str, float] = {}   # session -> ts of last title change

# Manual section overrides (in-memory only — never persisted). Maps a stable
# per-session key (session_id preferred, tmux name fallback — matches app's
# _key()) to (target_section, t0). An override pins the row to `target` until
# the session shows NEW activity (title change or new transcript ts) past t0,
# at which point gather_agents auto-clears it and reverts to live classification.
_section_overrides: dict[str, tuple[str, float]] = {}  # key -> (target_section, t0)


def set_section_override(key: str, target: str) -> None:
    """Pin a session to `target` section until new activity. Stamps t0=now."""
    _section_overrides[key] = (target, time.time())


def clear_section_override(key: str) -> None:
    _section_overrides.pop(key, None)


def find_transcript(session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    matches = glob.glob(PROJECTS_GLOB.format(sid=session_id))
    if not matches:
        return None
    # If multiple, take the most recently modified.
    return max(matches, key=lambda p: _safe_mtime(p))


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _tail_lines(path: str, nbytes: int, max_lines: Optional[int] = None) -> list[str]:
    """Read the last `nbytes` of a file and return complete, decoded JSON-ish
    lines in chronological order.

    Never reads more than `nbytes`. Drops a leading partial line caused by
    seeking into the middle of a record. SKIPS any single line larger than
    MAX_LINE_BYTES — those are attachment/image blobs (often hundreds of KB)
    that are never rendered as dialogue and would otherwise blow past the read
    window and starve the real conversation turns. If `max_lines` is given,
    only the last `max_lines` surviving lines are returned.
    """
    try:
        size = os.path.getsize(path)
        to_read = min(size, nbytes)
        with open(path, "rb") as f:
            if to_read < size:
                f.seek(size - to_read)
            data = f.read(to_read)
    except OSError:
        return []
    if to_read < size:
        nl = data.find(b"\n")  # drop leading partial line
        data = data[nl + 1:] if nl != -1 else b""
    out: list[str] = []
    for ln in data.split(b"\n"):
        if not ln.strip():
            continue
        if len(ln) > MAX_LINE_BYTES:
            continue  # giant attachment/image record — skip
        out.append(ln.decode("utf-8", errors="replace"))
    if max_lines is not None:
        out = out[-max_lines:]
    return out


def _iter_parsed(lines: list[str]):
    for ln in lines:
        try:
            yield json.loads(ln)
        except Exception:
            continue


# --- block / content extraction helpers (shared by list + preview) ---

def _content_blocks(obj: dict) -> list:
    msg = obj.get("message")
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, list):
            return c
        if isinstance(c, str):
            return [{"type": "text", "text": c}]
    c = obj.get("content")
    if isinstance(c, list):
        return c
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return []


def _role(obj: dict) -> str:
    t = obj.get("type")
    if t in ("assistant", "user"):
        # entries sometimes carry both; the explicit message.role wins.
        msg = obj.get("message")
        if isinstance(msg, dict) and msg.get("role"):
            return msg["role"]
        return t
    msg = obj.get("message")
    if isinstance(msg, dict):
        return msg.get("role", "")
    return ""


def _first_text(blocks: list) -> str:
    parts = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            txt = b.get("text", "")
            if isinstance(txt, str):
                parts.append(txt)
    return "\n".join(parts).strip()


def _summarize_tool(b: dict) -> str:
    """Short, privacy-light one-line summary of a tool_use block."""
    name = b.get("name", "tool")
    inp = b.get("input", {}) or {}
    if not isinstance(inp, dict):
        return name
    if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        base = os.path.basename(fp) if fp else ""
        return f"{name}  {base}".strip()
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().splitlines()
        first = cmd[0] if cmd else ""
        return f"{name}  {_clip(first, 70)}"
    if name in ("Read", "Glob", "Grep"):
        target = (inp.get("file_path") or inp.get("pattern")
                  or inp.get("path") or "")
        return f"{name}  {_clip(str(target), 60)}"
    if name in ("Task", "Agent"):
        desc = inp.get("description") or inp.get("subagent_type") or ""
        return f"{name}  {_clip(str(desc), 50)}"
    # generic: show first scalar field value, clipped
    for k in ("description", "query", "prompt", "url"):
        if inp.get(k):
            return f"{name}  {_clip(str(inp[k]), 50)}"
    return name


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _preview_text(s: str) -> str:
    """Return a full dialogue turn for the SCROLLABLE preview pane — IN FULL.

    Unlike _clip (which collapses ALL whitespace and hard-truncates for the
    one-line card snippet), this returns the COMPLETE turn text with internal
    newlines preserved (only the ends trimmed). NO truncation, NO ellipsis —
    Kiran wants the actual message in its entirety, and the pane scrolls. Any
    turn that reaches here is already inherently bounded: the upstream line read
    (_tail_lines) skips any record larger than MAX_LINE_BYTES, so giant
    attachment/image blobs never get this far.
    """
    return (s or "").strip()


def _parse_iso(ts: str) -> Optional[float]:
    if not ts or not isinstance(ts, str):
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


# --- list-level (cheap) parse ---

def parse_transcript_tail(path: str) -> dict:
    """Cheap tail parse for the LIST. Cached on (path, mtime).

    Returns { 'last_ts': float|None, 'snippet': str, 'pending_question': str }.
      - last_ts: timestamp of the last meaningful entry (for age + working).
      - snippet: last assistant text, or a tool-call summary, truncated.
      - pending_question: best-effort last assistant text (used for needy rows).
    """
    mtime = _safe_mtime(path)
    cached = _list_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    lines = _tail_lines(path, LIST_TAIL_BYTES, max_lines=300)
    objs = list(_iter_parsed(lines))

    last_ts: Optional[float] = None
    last_assistant_text = ""
    last_assistant_tool = ""
    for obj in objs:
        ts = _parse_iso(obj.get("timestamp", ""))
        if ts is not None:
            last_ts = ts  # objs are in order; last wins
        if _role(obj) == "assistant":
            blocks = _content_blocks(obj)
            txt = _first_text(blocks)
            if txt:
                last_assistant_text = txt
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    last_assistant_tool = _summarize_tool(b)

    snippet = last_assistant_text or last_assistant_tool or ""
    snippet = _clip(snippet.replace("\n", " "), 120)

    result = {
        "last_ts": last_ts,
        "snippet": snippet,
        "pending_question": _clip(last_assistant_text.replace("\n", " "), 160),
    }
    _list_cache[path] = (mtime, result)
    return result


# --- preview-level (deep) parse ---

def parse_transcript_preview(path: str, max_events: int = 24) -> list[dict]:
    """Deep-ish parse for the SELECTED agent's preview pane.

    DIALOGUE-ONLY: returns just the back-and-forth conversation — what Kiran
    sent and the prose the LLM wrote back. NO tool mechanics. Reads a larger
    tail and returns an ordered list of render events:
      {'kind': 'user'|'system'|'assistant', 'text': str}
    The caller styles them (blue ❯ marker for genuine user prose, yellow ⚙ marker
    for injected/system content that arrives as role=user but isn't Kiran's prose,
    plain wrapped bright text for assistant). tool_use blocks, tool_result-only
    user turns, and meta/command-wrapper noise are all skipped. Only the last
    `max_events` events are returned (newest at the end).
    """
    lines = _tail_lines(path, PREVIEW_TAIL_BYTES, max_lines=400)
    objs = list(_iter_parsed(lines))
    events: list[dict] = []

    for obj in objs:
        role = _role(obj)
        blocks = _content_blocks(obj)

        if role == "user":
            # Split role=user turns three ways: pure noise (hidden), injected/
            # system content rendered yellow (task-notifications, context
            # injections — NOT Kiran's prose), and genuine user prose (blue).
            txt = _first_text(blocks)
            if not txt:
                continue  # tool_result-only turn (no text) — skip
            if _looks_like_meta(txt):
                continue  # pure noise (system-reminder / command wrapper / caveat) — hide
            if _looks_like_injected(txt):
                events.append({"kind": "system", "text": _clip(txt, 160)})
            else:
                events.append({"kind": "user", "text": _preview_text(txt)})

        elif role == "assistant":
            # Keep only the assistant's TEXT prose; skip tool_use blocks.
            txt = _first_text(blocks)
            if txt:
                events.append({"kind": "assistant", "text": _preview_text(txt)})

    return events[-max_events:]


def _looks_like_meta(txt: str) -> bool:
    """Filter out command-wrapper / system-reminder noise from user turns."""
    head = txt.lstrip()[:30].lower()
    return head.startswith(("<command", "<local-command", "<system-reminder",
                            "caveat:", "[request interrupted"))


def _looks_like_injected(txt: str) -> bool:
    """True for injected/system content that is NOT Kiran's own prose but is worth
    surfacing in the preview (rendered yellow, not blue). Task-notifications and
    skill / base-directory context injections arrive as role=user; without this
    they'd render with the blue ❯ user marker and look like Kiran sent them."""
    head = txt.lstrip()[:48].lower()
    return head.startswith((
        "<task-notification", "<task_notification",
        "base directory", "the base directory for this skill",
        "this session is being continued",
    ))


# ---------------------------------------------------------------------------
# State derivation helpers
# ---------------------------------------------------------------------------

def prettify(session: str) -> str:
    """Strip leading 'cc-' and trailing '-<digits>' (the launcher pid)."""
    n = session
    if n.startswith("cc-"):
        n = n[3:]
    # drop a trailing -<digits>
    if "-" in n:
        head, _, tail = n.rpartition("-")
        if tail.isdigit():
            n = head
    return n


def extract_pid(session: str) -> str:
    """Trailing digits after the last '-' of cc-<base>-<pid>, else ''."""
    if "-" in session:
        tail = session.rpartition("-")[2]
        if tail.isdigit():
            return tail
    return ""


def git_branch(cwd: str) -> str:
    """Current branch of cwd, or '' (non-repo / detached HEAD / error)."""
    if not cwd or not os.path.isdir(cwd):
        return ""
    out = _run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
               timeout=2.0).strip()
    if not out or out == "HEAD":  # detached HEAD -> skip
        return ""
    return out


def humanize_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    if s < 0:
        s = 0
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def humanize_resets_in(reset_epoch: Optional[int]) -> str:
    """Humanize a usage-limit reset epoch as 'Xm' / 'Xh' / 'Xh Ym'.

    Mirrors the statusline's 5h reset display. Returns '' when there is no
    reset, the reset has already elapsed, or the value is unusable.
    """
    if reset_epoch is None:
        return ""
    remaining = int(reset_epoch) - int(time.time())
    if remaining <= 0:
        return ""
    hours = remaining // 3600
    mins = (remaining % 3600) // 60
    if hours > 0:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{mins}m" if mins else "<1m"


def _parse_opt_float(v) -> Optional[float]:
    """Parse a tap value (string/number) to float; '' / None / bad -> None."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_opt_int(v) -> Optional[int]:
    """Parse a tap value (string/number) to int; '' / None / bad -> None."""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _clean_opt_str(v) -> Optional[str]:
    """Tap string -> str, mapping '' / None / literal 'null' -> None."""
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s == "null":
        return None
    return s


# ---------------------------------------------------------------------------
# Top-level gather
# ---------------------------------------------------------------------------

def gather_agents() -> list[Agent]:
    """Build the full Agent list for one refresh. Cheap: tail-reads + cache.

    Ordering: most-recent activity first (smallest age_seconds), with unknown-age
    agents sinking to the bottom. Neediness is conveyed by the red highlight + the
    → next-alert key, not by sort position (see the sort at the end).
    """
    now = time.time()
    sessions = list_sessions()
    attached_map = sessions_attached()
    panes_all = list_panes_all()
    # Session-based ctx join (see ISSUE 3): resolve each ctx file's stored pane
    # to the session that LIVES on it now. This is now only a FALLBACK — the
    # `pane` field is unreliable (recycled/foreign), so we prefer process truth.
    ctx_by_session = load_ctx_by_session(panes_all)

    # Process-truth ctx join (see MISATTRIBUTION fix): index taps by their own
    # session_id, and resolve each tmux session's REAL sessionId from the
    # sessions store via the pane's process subtree. Built once per refresh.
    ctx_by_sid = load_ctx_by_sid()
    _sessions_store = _load_sessions_store()
    _ps_parent = _ps_parent_map()
    _pane_pids = _pane_pid_map()

    # session -> [pane ids]
    sess_panes: dict[str, list[str]] = {}
    for pane, sess in panes_all:
        sess_panes.setdefault(sess, []).append(pane)

    agents: list[Agent] = []
    for sess in sessions:
        panes = sess_panes.get(sess, [])
        ap = active_pane(sess)

        # Claude session title from the ACTIVE pane. Capture the RAW title ONCE
        # (used both for the cleaned display title and the working-detection
        # spinner animation below) and derive the cleaned title from it.
        # Computed BEFORE the ctx join because the process-truth resolver uses
        # the cleaned title as a final tiebreaker. Most live sessions have one;
        # store None for empty/useless values so it neither becomes a card title
        # nor a title-match key.
        raw_title = pane_title(ap) if ap else ""
        ptitle_clean = clean_pane_title(raw_title)
        ptitle = (ptitle_clean
                  if ptitle_clean
                  and ptitle_clean.lower() not in _USELESS_TITLES
                  else None)

        # ctx join: resolve the REAL Claude sessionId by process truth (sessions
        # store pid ∩ the active pane's process subtree), then fetch ctx by that
        # sessionId. Fall back to the old (unreliable) pane-based join only when
        # process truth can't resolve a sessionId for this pane.
        resolved_sid = resolve_sid_for_pane(ap, ptitle_clean, _pane_pids,
                                            _sessions_store, _ps_parent)
        ctx = ctx_by_sid.get(resolved_sid) if resolved_sid else None
        if ctx is None:
            ctx = ctx_by_session.get(sess)   # fallback: old pane-based join

        session_id = ctx["session_id"] if ctx else None
        cwd = ctx["cwd"] if ctx else ""
        pct = ctx["pct"] if ctx else None
        ctx_ts = ctx["ts"] if ctx else 0
        # new per-session metadata (all default to None when ctx/keys absent)
        ctx_model = ctx.get("model") if ctx else None
        ctx_effort = ctx.get("effort") if ctx else None
        ctx_worktree = ctx.get("worktree") if ctx else None
        ctx_five_h_pct = ctx.get("five_h_pct") if ctx else None
        ctx_five_h_reset = ctx.get("five_h_reset") if ctx else None
        ctx_seven_d_pct = ctx.get("seven_d_pct") if ctx else None
        ctx_seven_d_reset = ctx.get("seven_d_reset") if ctx else None

        # aerospace window-id stamp from the active pane
        wid = pane_opt(ap, "@aerospace_wid") if ap else ""

        # "working" detection: Claude animates a spinner glyph in the RAW pane
        # title while generating, so a changed raw title since the last refresh
        # means the agent is actively working. We flag working for a short grace
        # window after the last observed change so a single missed tick doesn't
        # drop the spinner. First sight of a session has prev is None -> not
        # flagged (avoids a startup flash where every row spins).
        prev = _prev_raw_title.get(sess)
        if raw_title and prev is not None and raw_title != prev:
            _last_title_change[sess] = now
        _prev_raw_title[sess] = raw_title
        working = (now - _last_title_change.get(sess, 0.0)) <= TITLE_WORKING_GRACE_SECONDS

        project = os.path.basename(cwd) if cwd else prettify(sess)
        pid = extract_pid(sess)

        # task: git branch -> prettified session name
        branch = git_branch(cwd)
        task = branch if branch else prettify(sess)

        # attention?
        needy = session_needy(panes)

        # open-window state (a tmux client is attached to this session)
        attached = attached_map.get(sess, False)

        # transcript tail (cheap, cached)
        last_ts: Optional[float] = None
        snippet = ""
        pending = ""
        tpath = find_transcript(session_id)
        if tpath:
            parsed = parse_transcript_tail(tpath)
            last_ts = parsed["last_ts"]
            snippet = parsed["snippet"]
            pending = parsed["pending_question"]

        # age: now - last transcript ts, fallback ctx ts
        if last_ts is not None:
            age = now - last_ts
        elif ctx_ts:
            age = now - float(ctx_ts)
        else:
            age = None

        # live (auto) classification — needy wins; else "working" if the raw pane
        # title is animating (see working detection above); else idle. (age no
        # longer drives state.) Stored into live_state so a manual override can
        # be layered on top without losing what the classifier actually thinks.
        if needy:
            live_state = "needs-input"
        elif working:
            live_state = "working"
        else:
            live_state = "idle"

        # Manual section override: pin to `target` until NEW activity since the
        # move (a title change or a newer transcript ts past t0) auto-clears it.
        key = session_id or sess        # match app's _key(): id preferred, tmux name fallback
        ov = _section_overrides.get(key)
        if ov is not None:
            target, t0 = ov
            last_activity = max(_last_title_change.get(sess, 0.0), (last_ts or 0.0))
            if last_activity > t0:
                _section_overrides.pop(key, None)   # NEW activity -> revert to live
                state = live_state
                pinned = False
            else:
                state = target
                pinned = True
        else:
            state = live_state
            pinned = False

        # active vs inactive (the cleanup-candidate split): a session is active
        # if it's working or waiting on Kiran OR has an open window OR was used
        # recently; inactive otherwise (idle AND windowless AND stale).
        active = (state in ("needs-input", "working")) or attached or \
                 (age is not None and age <= ACTIVE_RECENT_SECONDS)

        # snippet for needy rows: prefer the pending question text
        row_snippet = snippet
        if needy and pending:
            row_snippet = pending

        agents.append(Agent(
            session=sess,
            session_id=session_id,
            active_pane=ap,
            project=project,
            task=task,
            pid=pid,
            cwd=cwd,
            pct=pct,
            state=state,
            live_state=live_state,
            pinned=pinned,
            attached=attached,
            active=active,
            age_seconds=age,
            snippet=row_snippet,
            aerospace_wid=wid or None,
            pane_title=ptitle,
            panes=panes,
            model=ctx_model,
            effort=ctx_effort,
            worktree=ctx_worktree,
            five_h_pct=ctx_five_h_pct,
            five_h_reset=ctx_five_h_reset,
            seven_d_pct=ctx_seven_d_pct,
            seven_d_reset=ctx_seven_d_reset,
        ))

    # Prune dead sessions from the title-animation caches so they don't grow
    # unbounded as sessions come and go.
    live = set(sessions)
    for d in (_prev_raw_title, _last_title_change):
        for k in [k for k in d if k not in live]:
            del d[k]

    # Group by section, top→bottom: needs-you → running → inactive (via
    # _SECTION_RANK from state); within each section, unknown-age sinks, then
    # newest activity on top. Stable between refreshes (now advances uniformly)
    # — a row only moves when it changes section or genuinely gets a new message.
    agents.sort(key=lambda a: (_SECTION_RANK.get(a.state, 2),
                               a.age_seconds is None,
                               a.age_seconds if a.age_seconds is not None else 0.0))
    return agents


# ---------------------------------------------------------------------------
# Context tab — mirror the ctx-monitor dashboard
#
# We REUSE the cockpit's own per-session resolution (gather_agents -> good NAME
# via process-truth pane_title, ctx% from the tap, DIR from cwd, session8 from
# the resolved sessionId), then JOIN the monitor's per-pane cycle STATE from
# monitor-state.json by session_id. That gives better NAMEs than the raw
# monitor's @cc_name while keeping the monitor's exact column set + wording.
# ---------------------------------------------------------------------------

def load_monitor_states_by_sid() -> dict[str, str]:
    """Read monitor-state.json -> { session_id: raw_state }.

    The monitor persists `{"saved_at": ..., "panes": {pane_id: {session_id,
    state, ...}}}`. We key by the record's session_id (not pane_id) so the join
    is robust to pane recycling. If two panes report the same session_id, last
    one wins (a harmless edge — the monitor keys cycles per pane). Missing file /
    bad JSON -> {} (the monitor may simply not be running).
    """
    try:
        with open(MONITOR_STATE_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    panes = data.get("panes")
    if not isinstance(panes, dict):
        return out
    for rec in panes.values():
        if not isinstance(rec, dict):
            continue
        sid = rec.get("session_id")
        state = rec.get("state")
        if sid and state:
            out[str(sid)] = str(state)
    return out


def request_state_reset(sid: str) -> tuple[bool, str]:
    """Ask the ctx-monitor daemon to clear a session's monitor ERROR by writing
    a per-sid reset flag the daemon watches and re-arms on.

    Mechanism (STDLIB ONLY — no tmux, no send_message_to_pane, no live-session
    touch): atomically create `<CTX_DIR>/reset-<sid>.flag` by writing a unique
    temp file in the same dir then os.replace()-ing it onto the final path. The
    daemon's _handle_reset_flags() picks it up on its next tick, transitions the
    matching pane ERROR -> ARMED, and deletes the flag. This clears ONLY the
    daemon's bookkeeping — the running Claude session is never compacted/cleared.

    Returns (True, msg) on success, (False, msg) on a missing sid or OSError.
    """
    if not sid:
        return (False, "no session id")
    # Defense-in-depth: sids are Claude UUIDs, but never let one path-escape
    # CTX_DIR via the flag filename. Strip any dir component and reject the
    # result if it's empty or still carries a path separator.
    sid = os.path.basename(sid)
    if not sid or os.sep in sid or (os.altsep and os.altsep in sid):
        return (False, "invalid sid")
    try:
        os.makedirs(CTX_DIR, exist_ok=True)
        final = os.path.join(CTX_DIR, "reset-{}.flag".format(sid))
        tmp = os.path.join(
            CTX_DIR, "reset-{}.flag.tmp.{}".format(sid, os.getpid())
        )
        with open(tmp, "w") as f:
            f.write(datetime.now(timezone.utc).isoformat())
        os.replace(tmp, final)
    except OSError as e:
        return (False, "reset failed: {}".format(e))
    return (True, "monitor error cleared — re-arming watcher")


def monitor_liveness() -> MonitorLiveness:
    """Read monitor.lock's stamped PID and test it with os.kill(pid, 0).

    The lock file holds the holder's bare PID (see ctx-monitor's
    acquire_instance_lock). os.kill(pid, 0) raises ProcessLookupError when the
    pid is dead and PermissionError when it's alive but not ours (still alive).
    Missing/empty/garbage lock -> not running.
    """
    try:
        with open(MONITOR_LOCK_FILE) as f:
            raw = f.read().strip()
    except OSError:
        return MonitorLiveness(alive=False, pid=None)
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return MonitorLiveness(alive=False, pid=None)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return MonitorLiveness(alive=False, pid=pid)
    except PermissionError:
        return MonitorLiveness(alive=True, pid=pid)
    except OSError:
        return MonitorLiveness(alive=False, pid=pid)
    return MonitorLiveness(alive=True, pid=pid)


def gather_context_rows(agents: Optional[list[Agent]] = None) -> list[ContextRow]:
    """Build the Context-tab rows: one per live agent, sorted by CONTEXT %
    DESCENDING (most urgent at top).

    Reuses `agents` (pass the already-gathered list to avoid a second tmux/ps
    sweep; gathers fresh if None). NAME/DIR/ctx%/session8 come from the cockpit's
    own resolution; STATE is joined from monitor-state.json by session_id, with a
    sensible default (watching) where the monitor has no record.
    """
    if agents is None:
        agents = gather_agents()
    states_by_sid = load_monitor_states_by_sid()

    rows: list[ContextRow] = []
    for a in agents:
        raw_state = (states_by_sid.get(a.session_id)
                     if a.session_id else None) or MONITOR_DEFAULT_STATE
        # NAME: the cockpit's resolved card title (cleaned pane_title) -> label.
        name = a.pane_title or a.label or "-"
        dir_name = os.path.basename(cwd_) if (cwd_ := a.cwd) else "-"
        session8 = a.session_id[:8] if a.session_id else "-"
        rows.append(ContextRow(
            pane=a.active_pane or "-",
            name=name,
            dir=dir_name or "-",
            session8=session8,
            pct=a.pct,
            state_label=MONITOR_STATE_LABELS.get(raw_state, raw_state),
            is_error=(raw_state == "ERROR"),
            sid=a.session_id,
        ))

    # Sort by CONTEXT % DESCENDING (highest first). Rows with no ctx% sink to the
    # bottom (treated as -1).
    rows.sort(key=lambda r: (r.pct if r.pct is not None else -1), reverse=True)
    return rows


def aerospace_windows() -> list[tuple[str, str, str]]:
    """Parse `aerospace list-windows --all` -> [(wid, app, title)].

    Output rows are pipe-delimited + padded, e.g.
        `66867 | Ghostty | ✳ Check WA MCP server access`
        `41382 | Ghostty | /opt/homebrew/bin/tmux`
    Titles are returned RAW (not glyph-stripped); the matcher cleans them.
    Returns [] on any failure (binary missing / aerospace down).
    """
    out = _run([AEROSPACE_BIN, "list-windows", "--all"])
    if not out:
        # fall back to PATH lookup if the hard-coded path failed
        out = _run(["aerospace", "list-windows", "--all"])
    rows: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        wid = parts[0].strip()
        app = parts[1].strip()
        title = "|".join(parts[2:]).strip()  # titles may contain '|'
        if not wid.isdigit():
            continue
        rows.append((wid, app, title))
    return rows


def match_wid_by_title(cleaned_name: str,
                       windows: Optional[list[tuple[str, str, str]]] = None
                       ) -> Optional[str]:
    """Best-effort window-id recovery by matching a cleaned pane_title against
    the cleaned title column of Ghostty windows in `aerospace list-windows`.

    Both sides are cleaned with clean_pane_title() (spinner glyph stripped) and
    compared case-insensitively + trimmed. Returns:
      - a wid string  when EXACTLY ONE Ghostty window matches,
      - AMBIGUOUS_WID when 2+ Ghostty windows match (do not guess a wrong one),
      - None          when 0 match or the name is empty.
    Only Ghostty rows are considered (Claude sessions live in Ghostty windows).
    """
    if not cleaned_name:
        return None
    if windows is None:
        windows = aerospace_windows()
    target = cleaned_name.strip().lower()
    if not target:
        return None
    matches: list[str] = []
    for wid, app, title in windows:
        if app != "Ghostty":
            continue
        if clean_pane_title(title).strip().lower() == target:
            matches.append(wid)
    if not matches:
        return None
    if len(matches) > 1:
        return AMBIGUOUS_WID
    return matches[0]


def resolve_wid(agent: Agent,
                windows: Optional[list[tuple[str, str, str]]] = None
                ) -> Optional[str]:
    """Resolve the aerospace window-id for an agent (the Enter-handler target).

    Precedence:
      1. STAMPED @aerospace_wid (collision-free; set on client attach), else a
         live re-read of @aerospace_wid off the active pane (stamp may have
         landed since gather). Either candidate is VALIDATED against the live
         aerospace window list and DROPPED if gone (closing a Ghostty window
         does not clear the stamp, so a dead id can linger) — a stale candidate
         falls through to the title match below rather than being trusted.
      2. TITLE-MATCH FALLBACK: match the agent's cleaned pane_title against the
         cleaned Ghostty window titles from `aerospace list-windows --all`.
           - exactly one match  -> that wid
           - 2+ matches          -> AMBIGUOUS_WID (caller refuses to focus)
           - no match            -> None ("no window mapping yet")
    """
    # candidate from the stamp, else a live re-read off the active pane (the
    # stamp may have landed since gather). EITHER can be STALE — closing a
    # Ghostty window does not clear @aerospace_wid, so a dead id lingers on the
    # pane. Validate against the live window list before trusting it, so we
    # never hand focus_window a gone wid ("could not focus … (gone?)").
    cand: Optional[str] = agent.aerospace_wid
    if not cand and agent.active_pane:
        cand = pane_opt(agent.active_pane, "@aerospace_wid") or None
    if cand:
        if windows is None:
            windows = aerospace_windows()
        if any(wid == cand for wid, _app, _title in windows):
            return cand
        # stale stamp -> ignore it; fall through to the title match below
    # title-match fallback (best-effort; many fleet sessions predate the stamp)
    if agent.pane_title:
        return match_wid_by_title(agent.pane_title, windows)
    return None


def focus_window(wid: str) -> bool:
    """`aerospace focus --window-id <wid>`. Returns True on success."""
    try:
        r = subprocess.run(
            ["aerospace", "focus", "--window-id", str(wid)],
            capture_output=True, text=True, timeout=4.0,
        )
        return r.returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# tmux session kill + self-detection (for the cockpit kill feature)
# ---------------------------------------------------------------------------

def current_tmux_session() -> Optional[str]:
    """The tmux session the COCKPIT process is genuinely running inside,
    found by PROCESS TRUTH — a tmux pane whose pane-process is an ancestor of
    our own pid — NOT by the inherited $TMUX/$TMUX_PANE env. Trusting that env
    is unsafe: a cockpit launched from a Ghostty with a stale inherited $TMUX
    (leftover agent-spawn environment) would otherwise resolve `tmux display
    -p '#S'` to a FOREIGN session and make the kill guard refuse legit kills
    ("can't kill the cockpit's own session"). Returns None when we're not
    actually inside any live tmux pane — then there is no self to protect and
    the guard correctly allows the kill.
    """
    try:
        me = str(os.getpid())
        parent = _ps_parent_map()                # pid -> ppid, string-keyed
        anc = set(_ancestors(me, parent))        # ancestor pids (strings), excl. self
        anc.add(me)                              # include our own pid
        out = _run(["tmux", "list-panes", "-a", "-F", "#{pane_pid}\t#{session_name}"])
        for line in out.splitlines():
            if "\t" not in line:
                continue
            pid_s, sess = line.split("\t", 1)
            pane_pid = pid_s.strip()
            if pane_pid and pane_pid in anc:
                return sess.strip() or None
    except Exception:
        return None
    return None


def send_message_to_pane(pane: str, text: str) -> tuple[bool, str]:
    """Send `text` to a tmux pane (the running Claude session) and submit it.

    The verified mechanics (do NOT deviate — empirically established):
      1. COPY-MODE GUARD: if the pane is scrolled into copy-mode
         (`#{pane_in_mode}` == "1"), refuse — send-keys would land in the
         scrollback, not the prompt. Caller exits copy-mode and retries.
      2. SINGLE-LINE (no embedded newline): `send-keys -l <text>` (literal, so
         no key-name interpretation) then a SEPARATE `send-keys Enter` to submit.
      3. MULTI-LINE (embedded newline): feed the raw text via stdin to
         `load-buffer -` (avoids ALL shell-quoting issues), then
         `paste-buffer -p` (bracketed paste -> embedded newlines stay SOFT, not
         submitted), then a SEPARATE `send-keys Enter` to submit the whole draft.

    Sending to a BUSY Claude is fine — it queues the input and auto-runs; we do
    NOT wait for idle, send Escape, or interrupt. Returns (True, "") on success,
    else (False, "<reason>").
    """
    if not pane:
        return (False, "no pane")

    # 1. copy-mode guard
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_in_mode}"],
            capture_output=True, text=True, timeout=4.0,
        )
    except Exception as e:  # noqa: BLE001
        return (False, f"tmux error: {e}")
    if r.returncode != 0:
        return (False, "pane not found")
    if r.stdout.strip() == "1":
        return (False, "pane is scrolled (copy-mode); exit it and retry")

    try:
        if "\n" in text:
            # multi-line: load the raw text into the paste buffer via stdin,
            # bracketed-paste it (soft newlines), then submit with one Enter.
            lb = subprocess.run(
                ["tmux", "load-buffer", "-"],
                input=text, text=True, capture_output=True, timeout=4.0,
            )
            if lb.returncode != 0:
                return (False, "load-buffer failed")
            pb = subprocess.run(
                ["tmux", "paste-buffer", "-p", "-t", pane],
                capture_output=True, text=True, timeout=4.0,
            )
            if pb.returncode != 0:
                return (False, "paste-buffer failed")
        else:
            # single-line: literal send-keys, then submit.
            sk = subprocess.run(
                ["tmux", "send-keys", "-t", pane, "-l", text],
                capture_output=True, text=True, timeout=4.0,
            )
            if sk.returncode != 0:
                return (False, "send-keys failed")
        # submit (SEPARATE call so the newline is a real Enter keypress).
        en = subprocess.run(
            ["tmux", "send-keys", "-t", pane, "Enter"],
            capture_output=True, text=True, timeout=4.0,
        )
        if en.returncode != 0:
            return (False, "send Enter failed")
    except Exception as e:  # noqa: BLE001
        return (False, f"tmux error: {e}")
    return (True, "")


def rename_claude_session(pane: str, new_name: str) -> tuple[bool, str]:
    """Rename the running Claude session by TYPING `/rename <new_name>` into it.

    CRITICAL: the slash command must be TYPED, not pasted. Claude Code only
    executes a leading-slash line as a command when it arrives as literal
    keystrokes; bracketed-paste (load-buffer/paste-buffer, as the multi-line
    path in send_message_to_pane uses) is treated as ordinary input and the
    command is NOT run. So we always use `send-keys -l` (literal) for the
    command text, then a SEPARATE `send-keys Enter` to submit — never a paste
    buffer. Reuses send_message_to_pane's pane-exists + copy-mode guards.

    Returns (True, "renamed to <name>") on success, else (False, "<reason>").
    """
    if not pane:
        return (False, "no pane")
    new_name = new_name.strip()
    if not new_name:
        return (False, "empty name")

    # copy-mode guard (mirrors send_message_to_pane): a scrolled pane would land
    # the keystrokes in the scrollback, not the prompt.
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane, "#{pane_in_mode}"],
            capture_output=True, text=True, timeout=4.0,
        )
    except Exception as e:  # noqa: BLE001
        return (False, f"tmux error: {e}")
    if r.returncode != 0:
        return (False, "pane not found")
    if r.stdout.strip() == "1":
        return (False, "pane is scrolled (copy-mode); exit it and retry")

    try:
        # Type the slash command literally (-l so '/'/spaces aren't key names).
        sk = subprocess.run(
            ["tmux", "send-keys", "-t", pane, "-l", "/rename " + new_name],
            capture_output=True, text=True, timeout=4.0,
        )
        if sk.returncode != 0:
            return (False, "send-keys failed")
        # Brief delay so Claude registers the typed command before the Enter
        # (the same ordering the user does by hand). SEPARATE call so Enter is a
        # real keypress that submits the command.
        time.sleep(0.15)
        en = subprocess.run(
            ["tmux", "send-keys", "-t", pane, "Enter"],
            capture_output=True, text=True, timeout=4.0,
        )
        if en.returncode != 0:
            return (False, "send Enter failed")
    except Exception as e:  # noqa: BLE001
        return (False, f"tmux error: {e}")
    return (True, f"renamed to {new_name}")


def kill_session(session_name: str) -> bool:
    """`tmux kill-session -t <session_name>`. Returns True on returncode 0.

    DESTRUCTIVE: this ends the Claude session running in that tmux session. The
    caller (the cockpit) MUST have already passed through the confirmation modal
    AND the self-session guard before invoking this. We never guard here — this
    is the raw primitive; refuse-to-kill-self lives at the call site so the
    guard is explicit and testable.
    """
    if not session_name:
        return False
    try:
        r = subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True, text=True, timeout=4.0,
        )
        return r.returncode == 0
    except Exception:
        return False


def recent_claude_dirs(cap=20):
    """Distinct dirs Kiran recently launched Claude Code from, newest first.

    Source: ~/.claude/history.jsonl (append-only; each line is JSON with an
    absolute-path `project` field + epoch-MILLISECOND `timestamp`). Streamed
    line-by-line (~2.3MB) tolerating bad lines. Keeps the newest timestamp per
    distinct project, sorts descending, drops non-existent dirs, returns first
    `cap`.
    """
    import json, os
    last = {}
    p = os.path.expanduser("~/.claude/history.jsonl")
    try:
        with open(p, errors="replace") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                proj, ts = d.get("project"), d.get("timestamp")
                if not proj or ts is None:
                    continue
                if proj not in last or ts > last[proj]:
                    last[proj] = ts
    except OSError:
        return []
    ordered = sorted(last, key=lambda k: last[k], reverse=True)
    return [d for d in ordered if os.path.isdir(d)][:cap]


def attach_session_window(session: str) -> tuple[bool, str]:
    """Open a new Ghostty window attached to an EXISTING detached tmux
    session (the Enter-on-windowless re-attach path). Does NOT create a
    session or a new claude — the session already exists. argv lists only,
    no shell=True. Returns (True, msg) on success, (False, reason) on failure.
    """
    # tmux binary path
    TMUX = "/opt/homebrew/bin/tmux"
    if not os.path.exists(TMUX):
        TMUX = shutil.which("tmux") or "tmux"

    # Verify the session is still alive before opening a window onto it
    r = subprocess.run([TMUX, "has-session", "-t", session],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return (False, "session no longer exists")

    # Open a new Ghostty instance attached to the existing session
    subprocess.run(
        ["open", "-na", "Ghostty", "--args", "-e", TMUX, "attach", "-t", session],
        capture_output=True, text=True
    )

    return (True, "opening {} in a new window".format(session))


def _tmux_bin() -> str:
    """Absolute tmux path, falling back to PATH lookup then bare 'tmux'."""
    tmux = "/opt/homebrew/bin/tmux"
    if not os.path.exists(tmux):
        tmux = shutil.which("tmux") or "tmux"
    return tmux


def _slugify_session(name: str) -> str:
    """tmux session slug: keep [A-Za-z0-9_-], others -> '-', strip, fall back."""
    slug_chars = []
    for ch in name:
        if ch.isalnum() or ch in ('_', '-'):
            slug_chars.append(ch)
        else:
            slug_chars.append('-')
    return ''.join(slug_chars).strip('-') or 'agent'


def _free_tmux_slug(slug_base: str, tmux: str) -> str:
    """Find a free session slug: slug_base, else slug_base-2, -3, … (cap 99)."""
    slug = slug_base
    for n in range(2, 100):
        r = subprocess.run([tmux, "has-session", "-t", slug],
                           capture_output=True, text=True)
        if r.returncode != 0:
            break  # slug is free
        slug = "{}-{}".format(slug_base, n)
    return slug


def _open_claude_window(slug, directory, inner_cmd, color, tmux=None):
    """Create a detached tmux session running `inner_cmd`, open Ghostty on it,
    and auto-/color it in the background. Shared by spawn_claude_window and
    restart_agent so the window-opening mechanics live in ONE place.

    `inner_cmd` is the zsh -lc payload (e.g. "exec claude …"); it is shell-
    quoted here. argv lists only, NO shell=True. Returns (True, "") on success,
    else (False, reason).
    """
    tmux = tmux or _tmux_bin()

    # Build the inner command string for zsh -lc
    cmd = "/bin/zsh -lc " + shlex.quote(inner_cmd)

    # Create the detached tmux session
    r = subprocess.run(
        [tmux, "new-session", "-d", "-s", slug, "-c", directory, cmd],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        return (False, "tmux new-session failed")

    # Open a new Ghostty instance attached to it
    subprocess.run(
        ["open", "-na", "Ghostty", "--args", "-e", tmux, "attach", "-t", slug],
        capture_output=True, text=True
    )

    # Auto-color: detached background helper that waits ~10s then sends /color
    helper = (
        "python3 -c 'import time;time.sleep(10)'; "
        "{tmux} send-keys -t {slug} -l '/color {color}'; "
        "{tmux} send-keys -t {slug} Enter"
    ).format(tmux=shlex.quote(tmux), slug=shlex.quote(slug), color=color)
    subprocess.Popen(["/bin/zsh", "-lc", helper], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return (True, "")


def spawn_claude_window(name, directory, model, color):
    """Spawn a new detached tmux session running claude, open Ghostty, auto-color.

    Returns (True, message) on success, (False, reason) on failure.
    Uses argv lists, NO shell=True anywhere.
    """
    # Validate directory
    if not os.path.isdir(directory):
        return (False, "no such directory")

    tmux = _tmux_bin()
    slug = _free_tmux_slug(_slugify_session(name), tmux)

    inner = "exec claude --model {} --name {}".format(model, shlex.quote(name))
    ok, reason = _open_claude_window(slug, directory, inner, color, tmux)
    if not ok:
        return (False, reason)

    return (True, "spawned {} in {} on {}".format(
        name,
        os.path.basename(directory.rstrip('/')) or directory,
        model
    ))


# Map Claude's statusline `model.display_name` (what Agent.model holds, e.g.
# "Opus 4.8") to the `--model` value spawn_claude_window passes successfully
# (the MODELS ids in app.py). Exact current models map to their full id; any
# other display string (older models like "Sonnet 4.5"/"Opus 4.1", or a future
# one) falls back to the family alias (first word lowercased) which `claude
# --model` also accepts ('opus'/'sonnet'/'haiku'/'fable'). Unknown -> None
# (caller omits --model).
_MODEL_DISPLAY_TO_ID = {
    "Opus 4.8": "claude-opus-4-8",
    "Sonnet 4.6": "claude-sonnet-4-6",
    "Haiku 4.5": "claude-haiku-4-5-20251001",
    "Fable 5": "claude-fable-5",
}
_MODEL_FAMILY_ALIASES = {"opus", "sonnet", "haiku", "fable"}


def _model_flag(display: Optional[str]) -> Optional[str]:
    """Resolve Agent.model (a display_name) to a `--model` value, or None."""
    if not display:
        return None
    display = display.strip()
    if display in _MODEL_DISPLAY_TO_ID:
        return _MODEL_DISPLAY_TO_ID[display]
    first = display.split()[0].lower() if display.split() else ""
    if first in _MODEL_FAMILY_ALIASES:
        return first
    return None


def restart_agent(session, cwd, model, session_id) -> tuple[bool, str]:
    """Kill the agent's tmux session and reopen a FRESH window that RESUMES the
    same Claude conversation (so it reloads newly-created skills while keeping
    the chat). Reuses the same tmux slug for continuity when it frees in time.

    Args mirror the Agent fields: `session` (tmux slug), `cwd` (abs path to run
    in), `model` (Agent.model display string, may be None), `session_id`
    (Claude conversation uuid, may be None). Returns (True, "restarted …") on
    success, else (False, reason). argv lists only, NO shell=True.
    """
    if not session:
        return (False, "no session")
    if not cwd or not os.path.isdir(cwd):
        return (False, "no such directory")

    tmux = _tmux_bin()

    # 1. kill the old session.
    if not kill_session(session):
        return (False, "failed to kill old session")

    # 2. reuse the SAME slug for continuity: poll until tmux frees it (kill is
    #    async-ish), up to ~1.5s. If it won't free, fall back to a bumped slug.
    slug = session
    freed = False
    for _ in range(15):
        r = subprocess.run([tmux, "has-session", "-t", slug],
                           capture_output=True, text=True)
        if r.returncode != 0:
            freed = True
            break
        time.sleep(0.1)
    if not freed:
        slug = _free_tmux_slug(session, tmux)

    # 3. build the resume command. --resume <uuid> reloads THIS conversation;
    #    without a uuid, --continue picks the most recent one in cwd. The model
    #    flag is omitted when it can't be resolved (claude then uses its default).
    parts = ["exec", "claude"]
    if session_id:
        parts += ["--resume", shlex.quote(session_id)]
    else:
        parts += ["--continue"]
    mflag = _model_flag(model)
    if mflag:
        parts += ["--model", shlex.quote(mflag)]
    inner = " ".join(parts)

    # 4. open the fresh window (fresh random color is fine; preservation N/A).
    color = random.choice(SPAWN_COLORS)
    ok, reason = _open_claude_window(slug, cwd, inner, color, tmux)
    if not ok:
        return (False, reason)

    return (True, "restarted {}".format(session))
