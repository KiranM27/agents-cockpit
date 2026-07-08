#!/usr/bin/env python3
"""ctx-monitor — watches tmux-resident Claude Code sessions' context usage and
drives a checkpoint cycle (save -> /compact -> reorient) when usage crosses T1.

Spec: DESIGN.md in this directory. python3 stdlib ONLY — no pip installs.
Run:  python3 ctx-monitor.py [--t1 35] [--t2 40] [--rearm 20] [--tick 5]
                             [--dry-run] [--state-dir /tmp/claude-ctx]
"""

import argparse
import datetime
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time
from collections import deque

# ---------------------------------------------------------------------------
# Configuration. Thresholds and the tick are CLI-overridable (--t1 --t2
# --rearm --tick --state-dir); the rest are constants per DESIGN.md.
# ---------------------------------------------------------------------------

DEFAULT_T1 = 35  # %: crossing this triggers the save sequence
DEFAULT_T2 = 40  # %: Escape backstop for runaway turns
DEFAULT_REARM = 20  # %: at/below this, compact is considered done
DEFAULT_TICK = 5.0  # seconds between polls
DEFAULT_STATE_DIR = "/tmp/claude-ctx"  # tap files + monitor-state.json + monitor.log

# Pattern matched in a pane while Claude Code is mid-turn. Pinned empirically in
# Task 16 against the installed Claude Code version — if the live capture shows
# a different spinner shape, update THIS pattern (tests reference it, so they
# keep passing).
#
# Installed CC (Opus 4.8 / "xhigh effort" UI, captured 2026-06-10) shows NO
# "esc to interrupt" hint. The running-turn spinner reads (glyph + word rotate
# each redraw):
#   "✢ Perusing… (3s · ↓ 123 tokens · thinking with xhigh effort)"
#   "✻ Hashing… (6s · ↑ 158 tokens · thinking with xhigh effort)"
#   "· Vibing… (3s · ↓ 110 tokens · thinking with xhigh effort)"
# The live spinner ALWAYS carries a "(Ns" timer; idle/completed lines read
# "Brewed for 39s" / "Cooked for 8s" with no such timer. A plain "… (" substring
# of the WHOLE pane false-positives on idle prose like "as discussed… (see
# above)" and would STALL the cycle in SAVE_SENT (idle never reached, /compact
# never fires). So require the STRUCTURE: U+2026 ellipsis, optional space, then
# "(<digits>s".
BUSY_PATTERN = re.compile(r"…\s*\(\d+s")  # "… (Ns" — live-spinner timer hint

# A leading "spinner glyph + space" prefix on a pane title: one or more leading
# non-alphanumeric chars (the Claude spinner: ✳ ⠂ ⠐ ⠠ ⠄ · • etc.) that are NOT
# one of the title-opening chars we want to keep ( ( [ / ), followed by
# whitespace. We only strip when whitespace follows, so a title that legitimately
# starts with punctuation but has no glyph+space prefix is never truncated
# mid-word. Mirrors the agents-cockpit cleaner so NAME values look identical.
_SPINNER_PREFIX = re.compile(r"^[^\w\s(\[/]+\s+")

# pane_title values that carry no useful session identity (the launcher itself,
# a bare shell, the unstyled default) — treated as "no name" so the NAME column
# falls back to DIR / "-" instead of showing terminal cruft.
_USELESS_TITLES = {
    "",
    "tmux",
    "/opt/homebrew/bin/tmux",
    "/usr/bin/tmux",
    "zsh",
    "bash",
    "-zsh",
    "fish",
    "claude",
}


def clean_pane_title(raw):
    """Strip the leading Claude spinner glyph (+ following whitespace) from a
    raw `#{pane_title}` and trim. Mirrors the agents-cockpit cleaner so NAME
    values come out identical-looking.

    Examples:
      "✳ checkpoint-rollback" -> "checkpoint-rollback"
      "⠂ agent-cockpit"       -> "agent-cockpit"
      "Claude Code"           -> "Claude Code"   (no glyph prefix)
      ""                      -> ""

    Returns "" for empty/whitespace input and for a title that is purely a
    useless/generic value (the launcher, a bare shell) so callers can fall back.
    Only a leading run of glyph-ish chars FOLLOWED BY whitespace is removed, so a
    title is never truncated mid-word."""
    if not raw:
        return ""
    s = raw.strip()
    m = _SPINNER_PREFIX.match(s)
    if m:
        s = s[m.end():]
    s = s.strip()
    if s.lower() in _USELESS_TITLES:
        return ""
    return s

IDLE_TICKS_REQUIRED = 2  # marker absent N consecutive ticks => idle
ESCAPE_MAX_ATTEMPTS = 2  # Escape keypresses per cycle, max
ESCAPE_GRACE_SECONDS = 30.0  # min spacing between Escape attempts
COMPACT_TIMEOUT_SECONDS = 300.0  # 5 min for pct to fall to REARM after /compact
SAVE_RESEND_GRACE_SECONDS = 60.0  # after a save resend, wait before re-verifying
SEND_TEXT_SLEEP = 0.3  # real sleep between literal text and Enter

STATE_FILE_NAME = "monitor-state.json"
LOG_FILE_NAME = "monitor.log"

# Single-instance flock lives ALONGSIDE this script, NOT in the state dir.
# state_dir is /tmp/claude-ctx, and macOS reaps untouched /tmp files after
# ~3 days — it would unlink the lock out from under a long-running daemon (see
# the "THE BUG THIS FIXES" block above acquire_instance_lock). A path derived
# from __file__ sits on a never-reaped volume and follows the tool if it moves.
LOCK_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(LOCK_DIR, ".monitor.lock")
# Old scheme's /tmp lock — best-effort unlinked at startup so it can't linger
# and mislead anything still poking at it.
LEGACY_LOCK_PATH = "/tmp/claude-ctx/monitor.lock"

# Per-pane state machine states (names fixed by DESIGN.md).
ARMED = "ARMED"
SAVE_SENT = "SAVE_SENT"
COMPACT_SENT = "COMPACT_SENT"
REORIENT_SENT = "REORIENT_SENT"
ERROR = "ERROR"

# Human-readable dashboard labels for the states above. ONLY the rendered
# STATE column uses these — the internal identifiers stay unchanged in code,
# log lines, and the persisted monitor-state.json (tests and resume logic
# key on them).
STATE_LABELS = {
    ARMED: "watching",
    SAVE_SENT: "checkpoint requested",
    COMPACT_SENT: "compacting",
    REORIENT_SENT: "reorienting",
    ERROR: "ERROR - needs attention",
}

# NAME column width: cleaned pane-title names are short identities
# ("checkpoint-rollback-1627" class); 18 chars keeps the whole table inside
# ~100 columns.
NAME_COL_WIDTH = 18

# Classification of an injected message's real queue lifecycle, read from the
# Claude Code session transcript (see save_message_state).
LIVE = "LIVE"  # in flight: queued, or picked up / executing -> never resend
DEAD = "DEAD"  # cancelled (e.g. by Escape) or never landed -> resend justified

# ---------------------------------------------------------------------------
# Message templates (DESIGN.md verbatim; {checkpoint} = absolute checkpoint
# path). Configurable here, at the top of the script.
# ---------------------------------------------------------------------------

SAVE_TEMPLATE = (
    "Hey — we're about to compact your context so you have more room to work "
    "with. Before that happens, use this opportunity to save your full "
    "execution state — everything you'd need to pick up exactly where you are "
    "— to `{checkpoint}`. Write it now, then wrap up your current step."
)

COMPACT_COMMAND = "/compact"

REORIENT_TEMPLATE = (
    "Your context was just compacted. Read `{checkpoint}` to reorient "
    "yourself, then continue from where you left off."
)

ANSI_RED = "\033[31m"
ANSI_RESET = "\033[0m"


def checkpoint_path(cwd, session_id):
    """`<cwd>/.cc-checkpoint-<sess8>.md`. sess8 = first 8 chars of session_id,
    so two panes sharing a cwd cannot collide."""
    return os.path.join(cwd, ".cc-checkpoint-{}.md".format(session_id[:8]))


# ---------------------------------------------------------------------------
# Transcript-aware save-message classification.
#
# THE BUG THIS FIXES: a large checkpoint can take >60s to write, and an agent
# often goes briefly idle between sub-turns BEFORE writing it. The old resend
# trigger ("idle 2 ticks + checkpoint missing -> resend") fired a spurious
# second save in that window. The fix: instead of guessing from idle+missing,
# read Claude Code's session transcript (JSONL) to learn the save message's
# REAL state and only resend when it is genuinely DEAD.
#
# Transcript event shapes — VERIFIED EMPIRICALLY on this machine across 1672
# live transcripts (Claude Code 2.1.x), which DIFFER from the original brief:
#   * enqueue  — `{"type":"queue-operation","operation":"enqueue",
#                  "timestamp":...,"content":"<full message text>"}`. ALWAYS
#                carries `content` (1418/1418 observed).
#   * remove / dequeue — `{"type":"queue-operation","operation":"remove"|
#                  "dequeue","timestamp":...}`. NEVER carry `content`
#                (0/889 dequeue, 0/486 remove). So a removal CANNOT be matched
#                to our message by content; it is correlated by TIMESTAMP after
#                our matched enqueue instead.
#   * popAll   — `{"type":"queue-operation","operation":"popAll",
#                  "timestamp":...,"content":"<text>"}`. Carries `content`
#                (36/36); means the whole queue was flushed AND run.
#   * queued_command attachment — `{"type":"attachment","attachment":{"type":
#                  "queued_command","prompt":"<text>"},"timestamp":...}`. This
#                is the single positive "it ran" signal; it co-fires (same
#                timestamp) with the dequeue-for-execution `remove`.
#
# WHAT ESCAPE DOES TO A QUEUED MESSAGE (LIVE FINDING — disposable tgate-e2e
# session, 2026-06-10, Claude Code 2.1.x; raw evidence in the report):
#   Pressing Escape on a BUSY pane that has a message queued INTERRUPTS the
#   running turn and then DEQUEUES AND EXECUTES the queued message — it does
#   NOT clear/cancel it. The observed transcript sequence was:
#       enqueue(PROBE) -> [assistant partial] -> user "[Request interrupted
#       by user]" (the Escape) -> dequeue (content-less) -> user{content==PROBE,
#       promptSource:"queued"} -> assistant (acknowledges PROBE).
#   So the executed-from-queue message surfaces as a real `user` event whose
#   message content == our text (promptSource "queued"). CRUCIALLY this path
#   emits NO `queued_command` attachment, so matching ONLY on the attachment
#   would misread this common Escape outcome as DEAD and fire a spurious
#   resend — exactly the bug class we're killing. We therefore ALSO treat a
#   matching `user` turn as a positive "it ran" (LIVE) signal.
#   (A truly cancelled message — e.g. the user deletes it from the queue, or a
#   queue clear with no execution — still shows a removal with NO matching
#   user/attachment/popAll and reads DEAD, earning a resend.)
#
# CRITICAL: JSONL timestamps are NOT monotonic in file order (33/1511 inversions
# observed) because streaming events flush slightly out of order. We therefore
# parse the `timestamp` field on every event and key all ordering on it — never
# on line order.
# ---------------------------------------------------------------------------


def _iso_to_epoch(ts):
    """Parse a transcript ISO8601 UTC timestamp (e.g. '2026-06-10T12:00:00.000Z')
    to epoch seconds (float). Returns None on anything unparseable."""
    if not isinstance(ts, str):
        return None
    try:
        # Python 3.9's fromisoformat doesn't accept a trailing 'Z'.
        normalized = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        return datetime.datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None


def _epoch_to_iso(epoch):
    """Inverse of _iso_to_epoch: epoch seconds -> ISO8601 UTC with a 'Z' suffix.
    Used by tests to build synthetic transcripts comparable with epoch since_ts."""
    dt = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(dt.microsecond // 1000)


def find_transcript(session_id):
    """Locate the session transcript via ~/.claude/projects/*/<session_id>.jsonl.
    session_id is globally unique, so the glob is unambiguous; we do NOT hardcode
    the cwd->projectdir encoding. Returns the path or None if not found."""
    if not session_id:
        return None
    pattern = os.path.expanduser(
        os.path.join("~/.claude/projects", "*", session_id + ".jsonl")
    )
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def _user_message_text(ev):
    """Extract the text of a `user` transcript event. Claude Code writes the
    message content either as a plain string or as a list of content blocks
    ({"type":"text","text":...}); join the text blocks. Returns "" if absent."""
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def save_message_state(transcript_path, save_text, since_ts):
    """Classify the real queue lifecycle of OUR injected save message.

    Scans only events with timestamp >= since_ts (the moment we last sent it),
    matching our message by exact content/prompt == save_text. Returns:
      LIVE — in flight: still queued (enqueue with no later removal), OR picked
             up for execution (a queued_command attachment, a popAll with our
             content, OR a `user` turn whose content == save_text — the
             Escape-then-execute path). Do NOT resend; keep waiting (covers a
             slow >60s checkpoint write).
      DEAD — removed/dequeued WITHOUT any execution signal (cancelled, e.g. by
             an Escape), OR no trace of the message at all since since_ts (never
             landed). Resend is justified.
    Fail-safe: an unreadable / missing transcript returns DEAD, so we fall back
    toward the old resend behavior rather than silently never resending.
    """
    if not transcript_path:
        return DEAD
    try:
        with open(transcript_path, "r", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return DEAD

    enqueued = False  # our message was enqueued at/after since_ts
    executed = False  # positive "it ran": queued_command attach / popAll match
    removed = False  # a removal (remove/dequeue/popAll) happened after since_ts
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        ep = _iso_to_epoch(ev.get("timestamp"))
        if ep is None or ep < since_ts:
            continue
        etype = ev.get("type")
        if etype == "queue-operation":
            op = ev.get("operation")
            content = ev.get("content")
            if op == "enqueue" and content == save_text:
                enqueued = True
            elif op == "popAll":
                # popAll carries content and flushes+runs the queue. A matching
                # content is a positive execution signal for our message; any
                # popAll is also a removal event.
                removed = True
                if content == save_text:
                    executed = True
            elif op in ("remove", "dequeue"):
                # Content-less; correlate by timestamp ordering (see header).
                removed = True
        elif etype == "attachment":
            att = ev.get("attachment")
            if (
                isinstance(att, dict)
                and att.get("type") == "queued_command"
                and att.get("prompt") == save_text
            ):
                executed = True
        elif etype == "user":
            # A queued message that gets dequeued-and-run (the Escape-then-
            # execute path) surfaces as a real `user` turn whose content equals
            # our text (promptSource "queued"). That is a positive execution
            # signal even though no queued_command attachment was emitted.
            if _user_message_text(ev) == save_text:
                executed = True

    if executed:
        return LIVE  # picked up / running — never resend (slow write is fine)
    if enqueued and not removed:
        return LIVE  # still sitting in the queue, untouched
    # Either: enqueued then removed with no execution (cancelled / Escape), or
    # no trace of our message at all since since_ts (never landed).
    return DEAD


# ---------------------------------------------------------------------------
# Single-instance lock.
#
# THE BUG THIS FIXES (original): two monitor instances ran concurrently against
# the same state dir. Both double-sent every checkpoint-cycle message to the
# panes, and both wrote the SAME fixed persist tmp file — the rename loser
# raised "[Errno 2] No such file or directory: '...json.tmp' -> '...json'"
# every tick. An exclusive flock at startup makes a second instance impossible.
#
# THE BUG THIS FIXES (lock location): the lock USED to live at
# <state_dir>/monitor.lock i.e. /tmp/claude-ctx/monitor.lock. The daemon writes
# it once at startup and never touches it again, but macOS reaps untouched /tmp
# files after ~3 days — so on a long-lived daemon the reaper UNLINKED the lock
# inode out from under the still-running process. The daemon kept its flock on
# the now-orphaned (deleted) inode, so a second instance happily created a
# brand-new lock inode and acquired ITS flock with zero contention → two
# daemons again. The fix: the lock now lives at LOCK_PATH (.monitor.lock
# alongside this script, derived from __file__), on a volume the reaper never
# touches and independent of state_dir entirely.
# ---------------------------------------------------------------------------


def acquire_instance_lock(lock_path=None):
    """Take an exclusive non-blocking flock on LOCK_PATH (.monitor.lock next to
    this script) and stamp our PID into it. Returns the open file object — the
    CALLER MUST KEEP IT REFERENCED for the process lifetime (closing the fd,
    including via GC, releases the flock). flock releases automatically on
    process death, so there is no stale-lock handling: a crashed monitor never
    wedges the next.

    The lock path is deliberately INDEPENDENT of state_dir: state_dir is in
    /tmp, which macOS reaps after ~3 days, and a reaped lock inode silently
    breaks single-instance enforcement (see the "THE BUG THIS FIXES (lock
    location)" block above). lock_path defaults to the stable LOCK_PATH; tests
    inject a temp path so they never touch the real install's lock.

    If the lock is already held, print a one-line error naming the holder PID
    (read from the lock file) to stderr and exit 1.
    """
    if lock_path is None:
        lock_path = LOCK_PATH
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    # O_RDWR|O_CREAT (no O_APPEND): create if missing, keep existing contents
    # readable so a refused second instance can report the holder's PID.
    f = os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644), "r+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = f.read().strip() or "unknown"
        f.close()
        print(
            "ctx-monitor: another instance is already running (pid {}); "
            "exiting".format(holder),
            file=sys.stderr,
        )
        raise SystemExit(1)
    # Lock acquired: stamp our PID for the error message above.
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


# ---------------------------------------------------------------------------
# Tmux interface. ALL tmux interaction in this tool goes through this class so
# the state machine is unit-testable with a fake (see FakeTmux in the tests).
# ---------------------------------------------------------------------------


class Tmux:
    def __init__(self, dry_run=False, log=None):
        self.dry_run = dry_run
        self.log = log or (lambda msg: None)

    def _run(self, *args):
        result = subprocess.run(
            ["tmux"] + list(args), capture_output=True, text=True, check=True
        )
        return result.stdout

    def list_panes(self):
        """Set of live pane ids across ALL sessions, e.g. {'%0', '%12'}."""
        out = self._run("list-panes", "-a", "-F", "#{pane_id}")
        return {line.strip() for line in out.splitlines() if line.strip()}

    def pane_names(self):
        """{pane_id: cleaned session name} for ALL panes in ONE tmux call (per
        render, not per pane). The name is derived from the LIVE `#{pane_title}`
        — Claude's own session title — cleaned of the leading spinner glyph the
        way the agents-cockpit does it (clean_pane_title), so NAME matches what
        the cockpit shows. (The old `@cc_name` user option came from the now-
        RETIRED /tag command and is unset on essentially every pane.) A useless/
        generic title (launcher, bare shell, empty) cleans to '' so the caller
        falls back to DIR. Tab-separated because titles never contain tabs while
        pane ids never contain anything but %digits."""
        out = self._run("list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}")
        names = {}
        for line in out.splitlines():
            if not line.strip():
                continue
            pane, _, title = line.partition("\t")
            names[pane] = clean_pane_title(title)
        return names

    def capture_pane(self, pane):
        return self._run("capture-pane", "-p", "-t", pane)

    def pane_in_mode(self, pane):
        """True when the pane is in copy-mode (or any other tmux mode).
        send-keys into a mode are consumed BY the mode (a human scrolling the
        scrollback), NEVER by Claude Code — observed live: a save message sent
        into copy-mode left zero transcript trace and the state machine
        wrongly escalated the pane to terminal ERROR. Callers must defer every
        send until this clears. '#{pane_in_mode}' renders 1 inside a mode."""
        out = self._run("display", "-p", "-t", pane, "#{pane_in_mode}")
        return out.strip() == "1"

    def send_text(self, pane, text):
        """Proven sequence: C-u (clear input box so stale text never corrupts
        the message), literal text, REAL sleep, Enter."""
        if self.dry_run:
            self.log("DRY-RUN send_text to {}: {!r}".format(pane, text))
            return
        self._run("send-keys", "-t", pane, "C-u")
        self._run("send-keys", "-t", pane, "-l", text)
        time.sleep(SEND_TEXT_SLEEP)
        self._run("send-keys", "-t", pane, "Enter")

    def send_key(self, pane, key):
        if self.dry_run:
            self.log("DRY-RUN send_key to {}: {}".format(pane, key))
            return
        self._run("send-keys", "-t", pane, key)


# ---------------------------------------------------------------------------
# Per-pane state
# ---------------------------------------------------------------------------


def new_pane_state(session_id):
    """Fresh per-pane state. Every value is JSON-serializable (persisted as-is
    to monitor-state.json on every transition)."""
    return {
        "session_id": session_id,
        "state": ARMED,
        "idle_streak": 0,
        "cycle_started_at": None,
        "checkpoint_path": None,
        "escape_attempts": 0,
        "last_escape_at": None,
        "save_resent": False,
        "save_resent_at": None,
        # Wall-clock epoch of the LAST save-message send for this cycle (initial
        # save, a resend, or an Escape). save_message_state keys its transcript
        # scan on this so we always re-evaluate against events AFTER our most
        # recent action (post-Escape, post-resend).
        "last_save_sent_at": None,
        "compact_sent_at": None,
        "compact_retried": False,
    }


# ---------------------------------------------------------------------------
# Monitor — per-pane checkpoint-cycle state machines over the tap files
# ---------------------------------------------------------------------------


class Monitor:
    def __init__(
        self,
        tmux,
        t1=DEFAULT_T1,
        t2=DEFAULT_T2,
        rearm=DEFAULT_REARM,
        state_dir=DEFAULT_STATE_DIR,
        clock=time.time,
    ):
        self.tmux = tmux
        self.t1 = t1
        self.t2 = t2
        self.rearm = rearm
        self.state_dir = state_dir
        self.clock = clock
        self.panes = {}  # pane_id -> per-pane state dict
        self.recent = deque(maxlen=8)  # recent log lines for the dashboard
        # Panes whose copy-mode deferral streak has already been logged: the
        # "deferring sends" line fires ONCE when a streak starts, not every
        # 5s tick. In-memory only — after a restart, one fresh line is fine.
        self._mode_deferred = set()
        # Transcript resolver: session_id -> transcript path (or None). An
        # attribute so tests can point it at a controlled fixture file.
        self.find_transcript = find_transcript
        os.makedirs(self.state_dir, exist_ok=True)
        self._load_state()

    # -- logging / persistence ----------------------------------------------

    def _log(self, msg):
        line = "[{}] {}".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        self.recent.append(line)
        with open(os.path.join(self.state_dir, LOG_FILE_NAME), "a") as f:
            f.write(line + "\n")

    def _state_file(self):
        return os.path.join(self.state_dir, STATE_FILE_NAME)

    def _persist(self):
        """Atomic write (tmp + os.replace) of the full per-pane state. The tmp
        name carries our PID: the startup flock already forbids a second
        instance, but a PID-unique tmp means even a rogue concurrent writer
        can never race our rename (the old fixed '.tmp' name let the loser's
        os.replace raise ENOENT every tick)."""
        data = {"saved_at": self.clock(), "panes": self.panes}
        tmp = "{}.tmp.{}".format(self._state_file(), os.getpid())
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._state_file())

    def _load_state(self):
        """Resume exactly where a previous monitor process was (restart
        mid-cycle must not double-send). Missing keys get defaults; idle_streak
        resets so 'idle' always means two ticks observed by THIS process."""
        self.panes = {}
        try:
            with open(self._state_file()) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        rearmed_any = False
        for pane, saved in data.get("panes", {}).items():
            st = new_pane_state(saved.get("session_id", ""))
            st.update(saved)
            st["idle_streak"] = 0
            if st["state"] == ERROR:
                # ERROR is terminal only for the life of the process that
                # declared it: a restart means a human (or a fix) intervened,
                # so give the pane a fresh ARMED chance instead of resuming a
                # dead end. The next T1 crossing re-initializes cycle fields.
                st["state"] = ARMED
                rearmed_any = True
                self._log("{}: was ERROR; reset to ARMED on restart".format(pane))
            self.panes[pane] = st
        if self.panes:
            self._log("resumed state for {} pane(s)".format(len(self.panes)))
        # Persist the re-arm so disk immediately matches memory. Without this,
        # the on-disk monitor-state.json keeps the stale ERROR until some
        # unrelated transition happens to persist — and the cockpit's Context
        # tab (which reads STATE from disk) would show ERROR forever despite
        # the restart having already re-armed the pane in memory.
        if rearmed_any:
            self._persist()

    def _transition(self, pane, st, new_state, reason):
        old = st["state"]
        st["state"] = new_state
        self._log("{}: {} -> {} ({})".format(pane, old, new_state, reason))
        self._persist()

    # -- tap files (written by the statusline tap, Component 1) ----------------

    TAP_REQUIRED_KEYS = ("session_id", "pct", "pane", "cwd", "ts")

    def read_taps(self):
        """Read every tap file in state_dir. Returns {pane_id: tap_dict},
        keeping only the newest-ts tap per pane (a pane that hosted /clear or
        a relaunch has one file per session id). Malformed / mid-write files
        are skipped for this tick; monitor-state.json is never a tap."""
        taps = {}
        for name in sorted(os.listdir(self.state_dir)):
            if not name.endswith(".json") or name == STATE_FILE_NAME:
                continue
            path = os.path.join(self.state_dir, name)
            try:
                with open(path) as f:
                    tap = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(tap, dict) or not all(
                k in tap for k in self.TAP_REQUIRED_KEYS
            ):
                continue
            # Validate VALUE types, not just key presence: a tap with a string/
            # null pct passes the presence guard, then `tap["pct"] < t1` raises
            # TypeError that escapes the per-pane loop and kills the daemon.
            # (isinstance(True, int) is True; bool pct/ts is acceptable here.)
            if (
                not isinstance(tap["pct"], (int, float))
                or not isinstance(tap["ts"], (int, float))
                or not isinstance(tap["cwd"], str)
                or not isinstance(tap["session_id"], str)
                or not isinstance(tap["pane"], str)
            ):
                continue
            current = taps.get(tap["pane"])
            if current is None or tap["ts"] > current["ts"]:
                taps[tap["pane"]] = tap
        return taps

    def prune(self, taps, live_panes):
        """Drop monitor state AND tap files whose tmux pane no longer exists.
        Liveness == pane existence (NEVER tap-file age — idle sessions render
        sparsely, so a stale ts is normal). Returns the filtered taps."""
        for pane in list(self.panes):
            if pane not in live_panes:
                del self.panes[pane]
                self._mode_deferred.discard(pane)  # don't leak streak flags
                self._log("{}: pane gone; state dropped".format(pane))
                self._persist()
        for pane in list(taps):
            if pane not in live_panes:
                tap_file = os.path.join(
                    self.state_dir, taps[pane]["session_id"] + ".json"
                )
                try:
                    os.remove(tap_file)
                except OSError:
                    pass
                del taps[pane]
        return taps

    # -- busy / idle detection -------------------------------------------------

    def _update_idle_streak(self, pane, st):
        """Capture the pane and maintain its idle streak. Returns True when the
        pane is busy RIGHT NOW (running-turn marker visible)."""
        try:
            content = self.tmux.capture_pane(pane)
        except Exception:
            content = ""
        busy = BUSY_PATTERN.search(content) is not None
        if busy:
            st["idle_streak"] = 0
        else:
            st["idle_streak"] += 1
        return busy

    def _is_idle(self, st):
        """Idle = marker absent IDLE_TICKS_REQUIRED consecutive ticks (guards
        against capture-during-redraw races)."""
        return st["idle_streak"] >= IDLE_TICKS_REQUIRED

    # -- copy-mode send guard ----------------------------------------------------

    def _defer_if_in_mode(self, pane):
        """Consult BEFORE every send. True = the pane is in copy-mode (or
        another tmux mode), so any send-keys would be eaten by the mode and
        never reach Claude Code — the caller must treat its action as
        NOT-taken (mutate no state) and retry on a later tick.

        Logs "deferring sends" once per deferral streak, not per tick; the
        streak flag resets the moment the pane is observed out of the mode."""
        try:
            in_mode = self.tmux.pane_in_mode(pane)
        except Exception:
            # Can't tell (pane racing away?): don't block — the send goes out
            # and tick()'s per-pane isolation absorbs a dead-pane failure.
            in_mode = False
        if in_mode:
            if pane not in self._mode_deferred:
                self._mode_deferred.add(pane)
                self._log("{}: pane in copy-mode; deferring sends".format(pane))
        else:
            self._mode_deferred.discard(pane)
        return in_mode

    # -- main loop body ----------------------------------------------------------

    def _handle_reset_flags(self):
        """Process cockpit-written `reset-<sid>.flag` files: clear a session's
        monitor ERROR by re-arming the matching pane's state machine.

        The cockpit's Context tab drops a `reset-<sid>.flag` into state_dir when
        Kiran asks to clear a session's monitor ERROR. We re-arm purely the
        daemon's OWN bookkeeping (ERROR -> ARMED) — we do NOT touch the live
        Claude session. A flag is ALWAYS consumed (deleted) after handling,
        whether it acted or was ignored, so it can never re-fire or accumulate.

        read_taps() filters to `*.json` and prune() only touches `*.json` tap
        files, so a `reset-*.flag` is never mistaken for a tap or pruned.
        """
        for path in glob.glob(os.path.join(self.state_dir, "reset-*.flag")):
            try:
                base = os.path.basename(path)
                sid = base
                if sid.startswith("reset-"):
                    sid = sid[len("reset-"):]
                if sid.endswith(".flag"):
                    sid = sid[: -len(".flag")]
                # Resolve the pane whose per-pane state carries this sid.
                matched = None
                for pane_id, st in self.panes.items():
                    if st.get("session_id") == sid:
                        matched = (pane_id, st)
                        break
                if matched is None:
                    self._log(
                        "reset flag for sid {} ignored (unknown sid)".format(
                            sid[:8]
                        )
                    )
                else:
                    pane_id, st = matched
                    if st["state"] in (ERROR, ARMED):
                        # ERROR (or an already-ARMED pane whose on-disk copy may
                        # still read ERROR from a not-yet-persisted restart
                        # re-arm) -> force ARMED. _transition persists, so the
                        # cockpit's disk-sourced STATE is synced to reality even
                        # when memory was ARMED but disk lagged at ERROR.
                        self._transition(
                            pane_id, st, ARMED, "manual reset via cockpit"
                        )
                    else:
                        # In-flight cycle (SAVE_SENT / COMPACT_SENT /
                        # REORIENT_SENT): do NOT abort a legitimate cycle. Just
                        # persist so disk matches memory (the pane is genuinely
                        # not in ERROR), and report the no-op.
                        self._persist()
                        self._log(
                            "reset flag for sid {} ignored (pane {} mid-cycle "
                            "in {}; synced to disk)".format(
                                sid[:8], pane_id, st["state"]
                            )
                        )
                # ALWAYS consume the flag (acted or ignored).
                try:
                    os.remove(path)
                except OSError:
                    pass
            except Exception as e:
                self._log("reset flag {} error: {}".format(path, e))
                continue

    def tick(self):
        """One poll cycle. Returns {pane: tap} for the dashboard renderer."""
        now = self.clock()
        # Process cockpit reset flags every tick, regardless of whether any taps
        # exist (a session in ERROR may have no fresh tap).
        self._handle_reset_flags()
        try:
            live_panes = self.tmux.list_panes()
        except Exception as exc:
            # tmux server hiccup: never prune everything on a transient
            # failure — skip the whole tick.
            self._log("tmux unavailable ({}); skipping tick".format(exc))
            return {}
        taps = self.prune(self.read_taps(), live_panes)
        for pane in sorted(taps):
            # Isolate ANY single-pane failure (a pane closing between the
            # list_panes() snapshot and a later send-keys makes the real Tmux
            # raise CalledProcessError, etc.). Without this, one dead pane kills
            # the whole unattended daemon. list_panes() itself is NOT wrapped
            # here (a tmux-server-down tick still skips the whole tick above).
            try:
                tap = taps[pane]
                st = self.panes.get(pane)
                if st is None or st["session_id"] != tap["session_id"]:
                    # New pane, or new session in the same pane (/clear,
                    # relaunch): reset to ARMED, abandon any stale cycle.
                    # (/compact keeps the session_id, so normal cycles are
                    # unaffected.)
                    if st is not None:
                        self._log(
                            "{}: session changed ({} -> {}); reset to ARMED".format(
                                pane,
                                str(st["session_id"])[:8],
                                str(tap["session_id"])[:8],
                            )
                        )
                        # The old session's tap file is never read again (this
                        # pane now hosts a new session); unlink it so files
                        # don't accumulate for the life of the pane.
                        try:
                            os.unlink(
                                os.path.join(
                                    self.state_dir, str(st["session_id"]) + ".json"
                                )
                            )
                        except OSError:
                            pass  # already gone
                    st = new_pane_state(tap["session_id"])
                    self.panes[pane] = st
                    self._persist()
                busy = self._update_idle_streak(pane, st)
                self._advance(pane, st, tap, busy, now)
            except Exception as e:
                self._log("{}: tick error: {}".format(pane, e))
                continue
        return taps

    # -- state machine (edge-triggered; the monitor itself is the queue) ---------

    def _advance(self, pane, st, tap, busy, now):
        # ERROR has no branch on purpose: terminal until monitor restart
        # (_load_state resets ERROR panes to ARMED) or session change.
        if st["state"] == ARMED:
            self._advance_armed(pane, st, tap, now)
        elif st["state"] == SAVE_SENT:
            self._advance_save_sent(pane, st, tap, busy, now)
        elif st["state"] == COMPACT_SENT:
            self._advance_compact_sent(pane, st, tap, now)
        elif st["state"] == REORIENT_SENT:
            # Only reachable when the monitor restarted between the reorient
            # send and the immediate re-arm: the reorient already went out, so
            # just re-arm. Never re-send here.
            self._transition(pane, st, ARMED, "resumed after reorient; re-armed")

    def _advance_armed(self, pane, st, tap, now):
        """ARMED -> SAVE_SENT on pct >= T1. The pct test happens ONLY here:
        one crossing == one cycle (the resend guarantee)."""
        if tap["pct"] < self.t1:
            return
        # Copy-mode gate BEFORE any mutation: stay ARMED untouched so the
        # crossing simply re-fires on the next tick once the mode exits.
        if self._defer_if_in_mode(pane):
            return
        st["cycle_started_at"] = now
        st["checkpoint_path"] = checkpoint_path(tap["cwd"], tap["session_id"])
        st["escape_attempts"] = 0
        st["last_escape_at"] = None
        st["save_resent"] = False
        st["save_resent_at"] = None
        st["last_save_sent_at"] = now
        st["compact_sent_at"] = None
        st["compact_retried"] = False
        # Lands as steering text if a turn is running; as a prompt if idle.
        self.tmux.send_text(
            pane, SAVE_TEMPLATE.format(checkpoint=st["checkpoint_path"])
        )
        self._transition(
            pane,
            st,
            SAVE_SENT,
            "pct {} >= T1 {}; save message sent".format(tap["pct"], self.t1),
        )

    def _checkpoint_ok(self, st):
        """Checkpoint exists at the expected path with mtime after cycle
        start (a leftover from a previous cycle does not count)."""
        try:
            return os.path.getmtime(st["checkpoint_path"]) >= st["cycle_started_at"]
        except OSError:
            return False

    def _save_message_state(self, st):
        """Transcript-derived state of OUR save message since we last sent it
        (initial / resend / Escape). LIVE = queued or executing (never resend);
        DEAD = cancelled or no-trace (resend justified). Fail-safe DEAD on a
        missing/unreadable transcript."""
        transcript = self.find_transcript(st["session_id"])
        save_text = SAVE_TEMPLATE.format(checkpoint=st["checkpoint_path"])
        return save_message_state(transcript, save_text, st["last_save_sent_at"])

    def _advance_save_sent(self, pane, st, tap, busy, now):
        # Escape backstop: a runaway turn climbing past T2 gets interrupted so
        # the queued save message can land. Max 2 attempts, 30s apart, loudly
        # logged. LIVE FINDING (see save_message_state header): Escape INTERRUPTS
        # the running turn and then DEQUEUES-AND-EXECUTES the queued save (it is
        # NOT cleared) — the save then reads LIVE via its matching `user` turn,
        # so the gate below correctly does NOT resend. We still bump
        # last_save_sent_at to the Escape time so the state check re-evaluates
        # against POST-Escape events (and if Escape ever DID clear the queue with
        # no execution, that reads DEAD and earns the resend).
        if (
            tap["pct"] >= self.t2
            and busy
            and st["escape_attempts"] < ESCAPE_MAX_ATTEMPTS
            and (
                st["last_escape_at"] is None
                or now - st["last_escape_at"] >= ESCAPE_GRACE_SECONDS
            )
        ):
            # Copy-mode gate: an Escape sent into a mode exits THE MODE, not
            # the running turn. Skip without burning an attempt or bumping
            # last_escape_at/last_save_sent_at; conditions re-fire next tick.
            if self._defer_if_in_mode(pane):
                return
            self.tmux.send_key(pane, "Escape")
            st["escape_attempts"] += 1
            st["last_escape_at"] = now
            st["last_save_sent_at"] = now
            self._log(
                "{}: ESCAPE sent (attempt {}/{}; pct {} >= T2 {})".format(
                    pane,
                    st["escape_attempts"],
                    ESCAPE_MAX_ATTEMPTS,
                    tap["pct"],
                    self.t2,
                )
            )
            self._persist()
            return
        # NOTE: no timeout for reaching idle below T2 — a long turn hovering at
        # 36-39% is fine; the save message is already queued/steering and will
        # land.
        if not self._is_idle(st):
            return
        if (
            st["save_resent"]
            and st["save_resent_at"] is not None
            and now - st["save_resent_at"] < SAVE_RESEND_GRACE_SECONDS
        ):
            return  # give the resent save message time to be acted on
        if self._checkpoint_ok(st):
            # Copy-mode gate: stay SAVE_SENT untouched; the verified
            # checkpoint re-verifies and /compact fires on a later tick.
            if self._defer_if_in_mode(pane):
                return
            # Pane is idle by construction here — /compact is only ever sent
            # to an idle pane (queued slash commands mid-turn risk being
            # consumed as literal text). send_text does C-u ... Enter.
            self.tmux.send_text(pane, COMPACT_COMMAND)
            st["compact_sent_at"] = now
            st["compact_retried"] = False
            self._transition(
                pane, st, COMPACT_SENT, "checkpoint verified; /compact sent"
            )
            return
        # Checkpoint still missing. Consult the transcript before resending:
        # a LIVE message (queued OR executing — INCLUDING a slow >60s checkpoint
        # write) is NEVER resent. Only a DEAD message (cancelled by Escape, or
        # never landed) earns a resend. This is the headline fix: no spurious
        # second save while the agent is genuinely working the first one.
        msg_state = self._save_message_state(st)
        if msg_state == LIVE:
            return  # in flight — keep waiting, do not resend
        # DEAD from here on. The copy-mode gate covers BOTH the resend and the
        # ERROR escalation below: keystrokes eaten by a mode are exactly how a
        # healthy pane's save reads DEAD (it never landed), so a pane must
        # NEVER progress toward ERROR while a human is scrolling it.
        if self._defer_if_in_mode(pane):
            return
        if not st["save_resent"]:
            self.tmux.send_text(
                pane, SAVE_TEMPLATE.format(checkpoint=st["checkpoint_path"])
            )
            st["save_resent"] = True
            st["save_resent_at"] = now
            st["last_save_sent_at"] = now
            st["idle_streak"] = 0
            self._log(
                "{}: checkpoint missing & save msg DEAD; re-sent once".format(pane)
            )
            self._persist()
        else:
            self._transition(
                pane, st, ERROR, "checkpoint missing & save msg still DEAD after resend"
            )

    def _advance_compact_sent(self, pane, st, tap, now):
        if tap["pct"] <= self.rearm:
            # Copy-mode gate: stay COMPACT_SENT (no transition); pct stays at
            # the fresh low value, so the reorient re-fires on a later tick.
            if self._defer_if_in_mode(pane):
                return
            # Compact completed (its re-render delivered a fresh low pct).
            self.tmux.send_text(
                pane, REORIENT_TEMPLATE.format(checkpoint=st["checkpoint_path"])
            )
            self._transition(
                pane,
                st,
                REORIENT_SENT,
                "pct {} <= REARM {}; reorient sent".format(tap["pct"], self.rearm),
            )
            # Immediately re-arm: the next crossing past T1 starts a new cycle.
            self._transition(pane, st, ARMED, "cycle complete; re-armed")
            return
        if now - st["compact_sent_at"] < COMPACT_TIMEOUT_SECONDS:
            return
        if st["compact_retried"]:
            self._transition(pane, st, ERROR, "compact timed out twice")
        elif self._is_idle(st):
            # Copy-mode gate: skip without mutating compact_sent_at /
            # compact_retried — the timeout condition re-fires next tick.
            if self._defer_if_in_mode(pane):
                return
            # /compact is only ever sent to an idle pane.
            self.tmux.send_text(pane, COMPACT_COMMAND)
            st["compact_sent_at"] = now
            st["compact_retried"] = True
            self._log("{}: compact timeout; /compact re-sent (retry 1/1)".format(pane))
            self._persist()
        # else: busy when the timeout fired — wait; the retry fires on the
        # first idle tick after.

    # -- dashboard ----------------------------------------------------------------

    def render_lines(self, taps):
        """Dashboard body: one row per live pane + the recent log tail.
        Column budget (~93 chars worst case, fits a 100-col terminal):
        # 2 + PANE 6 + NAME 18 + DIR 20 + SESSION 10 + CONTEXT 7 + STATE <=23."""
        try:
            # ONE tmux call per render for every pane's identity, derived from
            # the live #{pane_title} (cleaned like the agents-cockpit does).
            names = self.tmux.pane_names()
        except Exception:
            names = {}  # tmux hiccup: render nameless rather than crash
        lines = []
        lines.append(
            "ctx-monitor  T1={}  T2={}  REARM={}  {}".format(
                self.t1, self.t2, self.rearm, time.strftime("%H:%M:%S")
            )
        )
        header = "{:>2} {:<6} {:<18} {:<20} {:<10} {:>7}  {}".format(
            "#", "PANE", "NAME", "DIR", "SESSION", "CONTEXT", "STATE"
        )
        lines.append(header)
        lines.append("-" * 93)
        for serial, pane in enumerate(sorted(taps), start=1):
            tap = taps[pane]
            st = self.panes.get(pane)
            state = st["state"] if st else "?"
            # NAME from the cleaned pane title; fall back to the repo DIR name
            # when the title is empty/useless (so a nameless pane still shows
            # *something* identifying), then "-". Truncate so one long name
            # can't shear the table.
            dirname = os.path.basename(str(tap["cwd"]).rstrip("/"))
            name = (names.get(pane) or dirname or "-")[:NAME_COL_WIDTH]
            row = "{:>2} {:<6} {:<18} {:<20} {:<10} {:>6}%  {}".format(
                serial,
                pane,
                name,
                dirname[:20],
                str(tap["session_id"])[:8],
                int(tap["pct"]),
                STATE_LABELS.get(state, state),
            )
            if state == ERROR:
                row = ANSI_RED + row + ANSI_RESET
            lines.append(row)
        lines.append("")
        lines.extend(self.recent)
        return lines

    def render(self, taps):
        # \033[2J\033[H = clear screen + home cursor.
        print("\033[2J\033[H" + "\n".join(self.render_lines(taps)), flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Watch tmux Claude Code panes; drive a save -> /compact -> "
        "reorient checkpoint cycle when context usage crosses T1."
    )
    p.add_argument(
        "--t1",
        type=int,
        default=DEFAULT_T1,
        help="save-sequence threshold, pct (default %(default)s)",
    )
    p.add_argument(
        "--t2",
        type=int,
        default=DEFAULT_T2,
        help="Escape backstop threshold, pct (default %(default)s)",
    )
    p.add_argument(
        "--rearm",
        type=int,
        default=DEFAULT_REARM,
        help="compact considered done at/below this pct " "(default %(default)s)",
    )
    p.add_argument(
        "--tick",
        type=float,
        default=DEFAULT_TICK,
        help="poll interval, seconds (default %(default)s)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="log every would-be send instead of sending",
    )
    p.add_argument(
        "--state-dir",
        default=DEFAULT_STATE_DIR,
        help="tap/state/log directory (default %(default)s)",
    )
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # Single-instance guard BEFORE any state is touched: two monitors double-
    # send every cycle message and race each other's persists. The returned
    # file object must stay referenced for the whole daemon loop — dropping
    # it would release the flock. The lock lives next to this script (LOCK_PATH),
    # NOT in /tmp, so the /tmp reaper can't unlink it mid-run.
    lock_file = acquire_instance_lock()  # noqa: F841 (held open)
    # Best-effort sweep of the old /tmp lock from the pre-LOCK_PATH scheme so a
    # stale file can't linger and mislead anything. Harmless if already absent.
    try:
        os.unlink(LEGACY_LOCK_PATH)
    except OSError:
        pass
    tmux = Tmux(dry_run=args.dry_run)
    monitor = Monitor(
        tmux, t1=args.t1, t2=args.t2, rearm=args.rearm, state_dir=args.state_dir
    )
    tmux.log = monitor._log
    if args.dry_run:
        monitor._log("dry-run mode: sends are logged, not executed")
    try:
        while True:
            taps = monitor.tick()
            monitor.render(taps)
            time.sleep(args.tick)
    except KeyboardInterrupt:
        print("\nctx-monitor stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
