"""Unit tests for ctx-monitor.

Run:  cd /Users/kiran/.claude/tools/ctx-monitor && python3 -m unittest test_ctx_monitor -v

ctx-monitor.py has a hyphen in its name (fixed by the spec), so it cannot be
imported with a normal `import` statement; this shim loads it under the module
name ctx_monitor.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "ctx_monitor", os.path.join(_HERE, "ctx-monitor.py")
)
ctx_monitor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ctx_monitor)


class ConstantsTest(unittest.TestCase):
    def test_thresholds_match_spec(self):
        self.assertEqual(ctx_monitor.DEFAULT_T1, 35)
        self.assertEqual(ctx_monitor.DEFAULT_T2, 40)
        self.assertEqual(ctx_monitor.DEFAULT_REARM, 20)
        self.assertEqual(ctx_monitor.DEFAULT_TICK, 5.0)
        self.assertEqual(ctx_monitor.DEFAULT_STATE_DIR, "/tmp/claude-ctx")

    def test_state_names(self):
        self.assertEqual(ctx_monitor.ARMED, "ARMED")
        self.assertEqual(ctx_monitor.SAVE_SENT, "SAVE_SENT")
        self.assertEqual(ctx_monitor.COMPACT_SENT, "COMPACT_SENT")
        self.assertEqual(ctx_monitor.REORIENT_SENT, "REORIENT_SENT")
        self.assertEqual(ctx_monitor.ERROR, "ERROR")

    def test_retry_budgets_match_spec(self):
        self.assertEqual(ctx_monitor.ESCAPE_MAX_ATTEMPTS, 2)
        self.assertEqual(ctx_monitor.ESCAPE_GRACE_SECONDS, 30.0)
        self.assertEqual(ctx_monitor.COMPACT_TIMEOUT_SECONDS, 300.0)
        self.assertEqual(ctx_monitor.IDLE_TICKS_REQUIRED, 2)

    def test_checkpoint_path_uses_first_8_of_session_id(self):
        self.assertEqual(
            ctx_monitor.checkpoint_path("/Users/kiran/repo", "abcdef12-3456-7890"),
            "/Users/kiran/repo/.cc-checkpoint-abcdef12.md",
        )

    def test_templates_mention_checkpoint(self):
        save = ctx_monitor.SAVE_TEMPLATE.format(checkpoint="/x/.cc-checkpoint-aa.md")
        self.assertIn("/x/.cc-checkpoint-aa.md", save)
        self.assertIn("save your full execution state", save)
        self.assertEqual(ctx_monitor.COMPACT_COMMAND, "/compact")
        reorient = ctx_monitor.REORIENT_TEMPLATE.format(
            checkpoint="/x/.cc-checkpoint-aa.md"
        )
        self.assertIn("/x/.cc-checkpoint-aa.md", reorient)
        self.assertIn("reorient", reorient)


class BusyPatternTest(unittest.TestCase):
    """BUSY_PATTERN must match the live running-turn spinner (which always
    carries a `(Ns` timer) and NOT match idle/completed lines or ordinary
    prose that merely contains an ellipsis followed by a paren."""

    REAL_SPINNERS = [
        "✢ Perusing… (3s · ↓ 123 tokens · thinking with xhigh effort)",
        "✻ Hashing… (6s · ↑ 158 tokens · thinking with xhigh effort)",
        "· Vibing… (3s · ↓ 110 tokens · thinking with xhigh effort)",
    ]
    NOT_BUSY = [
        "Brewed for 39s",
        "Cooked for 8s",
        "⏵ accept edits on",
        "as discussed… (see above)",
        "the list… (apples, pears)",
    ]

    def test_matches_real_spinner_lines(self):
        for line in self.REAL_SPINNERS:
            self.assertIsNotNone(
                ctx_monitor.BUSY_PATTERN.search(line),
                "expected busy match for: {!r}".format(line),
            )

    def test_does_not_match_idle_or_prose(self):
        for line in self.NOT_BUSY:
            self.assertIsNone(
                ctx_monitor.BUSY_PATTERN.search(line),
                "expected NO busy match for: {!r}".format(line),
            )


class TmuxCommandTest(unittest.TestCase):
    """Verifies the exact tmux invocations, with subprocess and sleep mocked."""

    def setUp(self):
        run_patcher = mock.patch.object(ctx_monitor.subprocess, "run")
        sleep_patcher = mock.patch.object(ctx_monitor.time, "sleep")
        self.mock_run = run_patcher.start()
        self.mock_sleep = sleep_patcher.start()
        self.addCleanup(run_patcher.stop)
        self.addCleanup(sleep_patcher.stop)
        self.mock_run.return_value = mock.Mock(stdout="")

    def tmux_calls(self):
        return [list(c.args[0]) for c in self.mock_run.call_args_list]

    def test_list_panes_parses_pane_ids(self):
        self.mock_run.return_value = mock.Mock(stdout="%0\n%12\n")
        self.assertEqual(ctx_monitor.Tmux().list_panes(), {"%0", "%12"})
        self.assertEqual(
            self.tmux_calls(), [["tmux", "list-panes", "-a", "-F", "#{pane_id}"]]
        )

    def test_capture_pane(self):
        self.mock_run.return_value = mock.Mock(stdout="pane text\n")
        self.assertEqual(ctx_monitor.Tmux().capture_pane("%5"), "pane text\n")
        self.assertEqual(
            self.tmux_calls(), [["tmux", "capture-pane", "-p", "-t", "%5"]]
        )

    def test_send_text_uses_proven_sequence(self):
        ctx_monitor.Tmux().send_text("%5", "hello world")
        self.assertEqual(
            self.tmux_calls(),
            [
                ["tmux", "send-keys", "-t", "%5", "C-u"],
                ["tmux", "send-keys", "-t", "%5", "-l", "hello world"],
                ["tmux", "send-keys", "-t", "%5", "Enter"],
            ],
        )
        self.mock_sleep.assert_called_once_with(ctx_monitor.SEND_TEXT_SLEEP)

    def test_send_key(self):
        ctx_monitor.Tmux().send_key("%5", "Escape")
        self.assertEqual(
            self.tmux_calls(), [["tmux", "send-keys", "-t", "%5", "Escape"]]
        )

    def test_pane_names_one_call_from_pane_title_cleaned(self):
        # NAME now comes from the live #{pane_title}, cleaned of the leading
        # spinner glyph (agents-cockpit behavior). %1 carries a spinner-prefixed
        # session name; %2 is a bare launcher title that cleans to "".
        self.mock_run.return_value = mock.Mock(
            stdout="%1\t✳ worker-a\n%2\t/opt/homebrew/bin/tmux\n"
        )
        names = ctx_monitor.Tmux().pane_names()
        self.assertEqual(names, {"%1": "worker-a", "%2": ""})
        self.assertEqual(self.mock_run.call_count, 1)  # ONE tmux call
        self.assertEqual(
            self.tmux_calls(),
            [["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}"]],
        )

    def test_clean_pane_title_strips_spinner_and_drops_useless(self):
        # Spinner glyph + space is stripped (mirrors the agents-cockpit cleaner).
        self.assertEqual(
            ctx_monitor.clean_pane_title("✳ checkpoint-rollback"),
            "checkpoint-rollback",
        )
        self.assertEqual(
            ctx_monitor.clean_pane_title("⠂ agent-cockpit"), "agent-cockpit"
        )
        # Plain sentence titles (no glyph) are kept verbatim.
        self.assertEqual(ctx_monitor.clean_pane_title("Claude Code"), "Claude Code")
        # Useless / generic titles clean to "" so callers fall back to DIR.
        self.assertEqual(ctx_monitor.clean_pane_title("/opt/homebrew/bin/tmux"), "")
        self.assertEqual(ctx_monitor.clean_pane_title("zsh"), "")
        self.assertEqual(ctx_monitor.clean_pane_title(""), "")
        self.assertEqual(ctx_monitor.clean_pane_title(None), "")

    def test_pane_in_mode_true_when_display_renders_1(self):
        self.mock_run.return_value = mock.Mock(stdout="1\n")
        self.assertTrue(ctx_monitor.Tmux().pane_in_mode("%5"))
        self.assertEqual(
            self.tmux_calls(),
            [["tmux", "display", "-p", "-t", "%5", "#{pane_in_mode}"]],
        )

    def test_pane_in_mode_false_when_display_renders_0(self):
        self.mock_run.return_value = mock.Mock(stdout="0\n")
        self.assertFalse(ctx_monitor.Tmux().pane_in_mode("%5"))

    def test_dry_run_logs_instead_of_sending(self):
        logged = []
        t = ctx_monitor.Tmux(dry_run=True, log=logged.append)
        t.send_text("%5", "hello")
        t.send_key("%5", "Escape")
        self.assertEqual(self.mock_run.call_count, 0)
        self.assertEqual(len(logged), 2)
        self.assertIn("DRY-RUN", logged[0])
        self.assertIn("DRY-RUN", logged[1])


# ---------------------------------------------------------------------------
# Test doubles. FakeTmux implements the exact same interface as
# ctx_monitor.Tmux so the Monitor never touches real tmux in unit tests.
# ---------------------------------------------------------------------------


class FakeTmux:
    def __init__(self):
        self.panes = set()  # live pane ids, e.g. {"%1"}
        self.pane_content = {}  # pane id -> text returned by capture_pane
        self.in_mode_panes = set()  # panes currently in copy-mode
        self.names = {}  # pane id -> @cc_name value ("" when unset)
        self.sent_texts = []  # (pane, text) tuples, in send order
        self.sent_keys = []  # (pane, key) tuples, in send order
        self.log = lambda msg: None

    def list_panes(self):
        return set(self.panes)

    def pane_names(self):
        return dict(self.names)

    def capture_pane(self, pane):
        return self.pane_content.get(pane, "")

    def pane_in_mode(self, pane):
        return pane in self.in_mode_panes

    def send_text(self, pane, text):
        self.sent_texts.append((pane, text))

    def send_key(self, pane, key):
        self.sent_keys.append((pane, key))


class FakeClock:
    """Deterministic clock. Starts at the real epoch so checkpoint-file mtimes
    (real filesystem timestamps set via os.utime) stay comparable with fake
    'now' values."""

    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


SESSION = "sess1234abcd"  # default session id used across monitor tests


class MonitorTestCase(unittest.TestCase):
    """Shared scaffolding: temp state dir + work dir, fakes, tap helpers."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_dir = os.path.join(self.tmpdir.name, "claude-ctx")
        os.makedirs(self.state_dir)
        self.work_dir = os.path.join(self.tmpdir.name, "repo")
        os.makedirs(self.work_dir)
        self.tmux = FakeTmux()
        self.clock = FakeClock()
        # A realistic running-turn spinner line (carries the `(Ns` timer that
        # BUSY_PATTERN keys on). Used wherever a test needs a pane to read busy.
        self.busy = "✢ Perusing… (3s · ↓ 123 tokens · thinking with xhigh effort)"

    def make_monitor(self, **kwargs):
        return ctx_monitor.Monitor(
            self.tmux, state_dir=self.state_dir, clock=self.clock, **kwargs
        )

    def write_tap(self, session_id=SESSION, pct=10, pane="%1", cwd=None, ts=None):
        tap = {
            "session_id": session_id,
            "pct": pct,
            "pane": pane,
            "cwd": cwd if cwd is not None else self.work_dir,
            "ts": ts if ts is not None else self.clock.now,
        }
        with open(os.path.join(self.state_dir, session_id + ".json"), "w") as f:
            json.dump(tap, f)

    def write_checkpoint(self, session_id=SESSION, mtime=None):
        """Create the checkpoint file the save message asks the agent to write.
        Default mtime = clock.now + 1 (i.e. strictly after cycle start)."""
        path = ctx_monitor.checkpoint_path(self.work_dir, session_id)
        with open(path, "w") as f:
            f.write("# checkpoint\n")
        target = self.clock.now + 1 if mtime is None else mtime
        os.utime(path, (target, target))
        return path


class InstanceLockTest(unittest.TestCase):
    """acquire_instance_lock: exclusive flock on an injectable lock_path.
    The real install locks LOCK_PATH (.monitor.lock next to the script, off
    /tmp so the reaper can't unlink it); tests inject a temp path so they never
    touch that file. flock conflicts are per open-file-description, so a second
    acquire in the SAME process still conflicts — which is what lets us test the
    refusal in-process without spawning a second python."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # Injected lock path — NEVER the real install's LOCK_PATH.
        self.lock_path = os.path.join(self.tmpdir.name, ".monitor.lock")

    def test_acquire_creates_lock_and_stamps_pid(self):
        f = ctx_monitor.acquire_instance_lock(self.lock_path)
        self.addCleanup(f.close)
        with open(self.lock_path) as lf:
            self.assertEqual(lf.read().strip(), str(os.getpid()))

    def test_second_acquire_exits_1_naming_holder_pid(self):
        f = ctx_monitor.acquire_instance_lock(self.lock_path)
        self.addCleanup(f.close)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as cm:
                ctx_monitor.acquire_instance_lock(self.lock_path)
        self.assertEqual(cm.exception.code, 1)
        msg = stderr.getvalue()
        self.assertIn("already running", msg)
        self.assertIn(str(os.getpid()), msg)  # holder PID read from the file

    def test_released_lock_can_be_reacquired(self):
        # flock releases on close (and therefore on process death): no stale-
        # lock handling needed.
        ctx_monitor.acquire_instance_lock(self.lock_path).close()
        f2 = ctx_monitor.acquire_instance_lock(self.lock_path)
        self.addCleanup(f2.close)


class MonitorCoreTest(MonitorTestCase):
    def test_transition_persists_state_atomically(self):
        m = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        m.panes["%1"] = st
        m._transition("%1", st, ctx_monitor.SAVE_SENT, "unit test")
        state_file = os.path.join(self.state_dir, "monitor-state.json")
        with open(state_file) as f:
            data = json.load(f)
        self.assertEqual(data["panes"]["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.assertEqual(data["panes"]["%1"]["session_id"], SESSION)
        self.assertFalse(os.path.exists(state_file + ".tmp"))

    def test_load_restores_state_and_resets_idle_streak(self):
        m1 = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        st["idle_streak"] = 3
        m1.panes["%1"] = st
        m1._transition("%1", st, ctx_monitor.COMPACT_SENT, "unit test")
        m2 = self.make_monitor()
        self.assertEqual(m2.panes["%1"]["state"], ctx_monitor.COMPACT_SENT)
        self.assertEqual(m2.panes["%1"]["session_id"], SESSION)
        self.assertEqual(m2.panes["%1"]["idle_streak"], 0)

    def test_persist_tmp_path_carries_pid(self):
        # The tmp name must be PID-unique so two writers can never race the
        # rename (the dual-instance "tick error" bug).
        m = self.make_monitor()
        m.panes["%1"] = ctx_monitor.new_pane_state(SESSION)
        seen = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append((src, dst))
            return real_replace(src, dst)

        with mock.patch.object(ctx_monitor.os, "replace", side_effect=spy):
            m._persist()
        self.assertEqual(len(seen), 1)
        src, dst = seen[0]
        self.assertTrue(
            src.endswith(".tmp.{}".format(os.getpid())),
            "tmp path {!r} lacks PID suffix".format(src),
        )
        self.assertEqual(dst, os.path.join(self.state_dir, "monitor-state.json"))
        self.assertFalse(os.path.exists(src))  # consumed by the rename

    def test_transition_appends_log_line(self):
        m = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        m.panes["%1"] = st
        m._transition("%1", st, ctx_monitor.ERROR, "boom")
        with open(os.path.join(self.state_dir, "monitor.log")) as f:
            log = f.read()
        self.assertIn("ARMED -> ERROR", log)
        self.assertIn("boom", log)


class ReadTapsTest(MonitorTestCase):
    def test_reads_valid_tap_keyed_by_pane(self):
        m = self.make_monitor()
        self.write_tap(pct=37)
        taps = m.read_taps()
        self.assertEqual(set(taps), {"%1"})
        self.assertEqual(taps["%1"]["session_id"], SESSION)
        self.assertEqual(taps["%1"]["pct"], 37)
        self.assertEqual(taps["%1"]["cwd"], self.work_dir)

    def test_newest_ts_wins_when_two_sessions_share_a_pane(self):
        # A /clear or relaunch leaves the OLD session's tap file on disk with
        # the same pane id; the newest ts must win.
        m = self.make_monitor()
        self.write_tap(session_id="sessOLDxxxxx", pane="%1", ts=self.clock.now - 50)
        self.write_tap(session_id="sessNEWxxxxx", pane="%1", ts=self.clock.now)
        taps = m.read_taps()
        self.assertEqual(taps["%1"]["session_id"], "sessNEWxxxxx")

    def test_skips_malformed_files_and_monitor_state(self):
        m = self.make_monitor()
        with open(os.path.join(self.state_dir, "corrupt.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(self.state_dir, "incomplete.json"), "w") as f:
            json.dump({"session_id": "x"}, f)  # missing pct/pane/cwd/ts
        m._persist()  # creates monitor-state.json
        self.assertEqual(m.read_taps(), {})

    def test_skips_tap_with_wrong_value_types(self):
        # Keys present but wrong types: a string pct passes a presence-only
        # guard, then `tap["pct"] < t1` raises TypeError that kills the daemon.
        m = self.make_monitor()
        bad = {
            "session_id": "s",
            "pane": "%1",
            "cwd": "/x",
            "pct": "abc",  # string, not a number
            "ts": 123,
        }
        with open(os.path.join(self.state_dir, "badtype.json"), "w") as f:
            json.dump(bad, f)
        self.assertEqual(m.read_taps(), {})

    def test_tick_does_not_raise_on_bad_type_tap(self):
        # A full tick over a dir containing the malformed tap must not raise.
        self.tmux.panes = {"%1"}
        m = self.make_monitor()
        bad = {
            "session_id": "s",
            "pane": "%1",
            "cwd": "/x",
            "pct": "abc",
            "ts": 123,
        }
        with open(os.path.join(self.state_dir, "badtype.json"), "w") as f:
            json.dump(bad, f)
        taps = m.tick()  # must not raise
        self.assertEqual(taps, {})


class PruneTest(MonitorTestCase):
    def test_dead_pane_drops_state_and_tap_file(self):
        m = self.make_monitor()
        m.panes["%1"] = ctx_monitor.new_pane_state(SESSION)
        self.write_tap(pane="%1")
        taps = m.prune(m.read_taps(), live_panes=set())
        self.assertEqual(taps, {})
        self.assertNotIn("%1", m.panes)
        self.assertFalse(
            os.path.exists(os.path.join(self.state_dir, SESSION + ".json"))
        )

    def test_live_pane_untouched(self):
        m = self.make_monitor()
        m.panes["%1"] = ctx_monitor.new_pane_state(SESSION)
        self.write_tap(pane="%1")
        taps = m.prune(m.read_taps(), live_panes={"%1"})
        self.assertIn("%1", taps)
        self.assertIn("%1", m.panes)
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, SESSION + ".json")))


class IdleStreakTest(MonitorTestCase):
    def test_idle_requires_two_consecutive_clear_ticks(self):
        self.tmux.panes = {"%1"}
        m = self.make_monitor()
        self.write_tap(pct=10)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        self.assertEqual(m.panes["%1"]["idle_streak"], 0)
        self.tmux.pane_content["%1"] = "idle prompt"
        m.tick()
        self.assertEqual(m.panes["%1"]["idle_streak"], 1)
        self.assertFalse(m._is_idle(m.panes["%1"]))
        m.tick()
        self.assertEqual(m.panes["%1"]["idle_streak"], 2)
        self.assertTrue(m._is_idle(m.panes["%1"]))
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        self.assertEqual(m.panes["%1"]["idle_streak"], 0)


class ArmedStateTest(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.tmux.panes = {"%1"}

    def test_below_t1_no_send(self):
        m = self.make_monitor()
        self.write_tap(pct=34)
        m.tick()
        self.assertEqual(self.tmux.sent_texts, [])
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ARMED)

    def test_crossing_t1_sends_save_once_and_records_cycle(self):
        m = self.make_monitor()
        self.write_tap(pct=36)
        m.tick()
        self.assertEqual(len(self.tmux.sent_texts), 1)
        pane, text = self.tmux.sent_texts[0]
        self.assertEqual(pane, "%1")
        expected_ckpt = ctx_monitor.checkpoint_path(self.work_dir, SESSION)
        self.assertIn(expected_ckpt, text)
        self.assertIn("save your full execution state", text)
        st = m.panes["%1"]
        self.assertEqual(st["state"], ctx_monitor.SAVE_SENT)
        self.assertEqual(st["checkpoint_path"], expected_ckpt)
        self.assertEqual(st["cycle_started_at"], self.clock.now)
        self.assertEqual(st["escape_attempts"], 0)

    def test_edge_triggered_no_resend_while_above_t1(self):
        m = self.make_monitor()
        self.write_tap(pct=36)
        m.tick()
        self.write_tap(pct=38)
        # busy pane: the idle/verify path (Task 8) can't fire either
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        m.tick()
        self.assertEqual(len(self.tmux.sent_texts), 1)


class SaveSentTest(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.tmux.panes = {"%1"}

    def reach_save_sent(self, m):
        self.write_tap(pct=36)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)

    def go_idle(self, m):
        self.tmux.pane_content["%1"] = "idle prompt"
        m.tick()
        m.tick()

    def save_sends(self):
        return [
            t for t in self.tmux.sent_texts if "save your full execution state" in t[1]
        ]

    def test_checkpoint_verified_sends_compact(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        self.write_checkpoint()  # mtime after cycle start
        self.go_idle(m)
        st = m.panes["%1"]
        self.assertEqual(st["state"], ctx_monitor.COMPACT_SENT)
        self.assertEqual(self.tmux.sent_texts[-1], ("%1", "/compact"))
        self.assertEqual(st["compact_sent_at"], self.clock.now)

    def test_not_idle_no_verification(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        self.write_checkpoint()
        m.tick()  # still busy: streak 0
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.tmux.pane_content["%1"] = "idle prompt"
        m.tick()  # streak 1: not idle yet
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)

    def test_stale_checkpoint_treated_as_missing(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        self.write_checkpoint(mtime=self.clock.now - 100)  # before cycle start
        self.go_idle(m)
        st = m.panes["%1"]
        self.assertEqual(st["state"], ctx_monitor.SAVE_SENT)
        self.assertTrue(st["save_resent"])
        self.assertEqual(len(self.save_sends()), 2)  # initial + resend

    def test_missing_checkpoint_resends_save_once(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        self.go_idle(m)  # idle, no checkpoint -> resend
        st = m.panes["%1"]
        self.assertEqual(st["state"], ctx_monitor.SAVE_SENT)
        self.assertTrue(st["save_resent"])
        self.assertEqual(st["idle_streak"], 0)  # reset by resend
        self.assertEqual(len(self.save_sends()), 2)

    def test_no_verification_during_resend_grace(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        self.go_idle(m)  # resend fired, clock unchanged
        self.go_idle(m)  # within grace: must NOT go ERROR
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.assertEqual(len(self.save_sends()), 2)

    def test_still_missing_after_resend_goes_error(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        self.go_idle(m)  # resend
        self.clock.advance(ctx_monitor.SAVE_RESEND_GRACE_SECONDS + 1)
        self.go_idle(m)  # grace over, still missing
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ERROR)

    def test_resend_then_checkpoint_appears_recovers(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        self.go_idle(m)  # resend
        self.write_checkpoint()  # agent writes it now
        self.clock.advance(ctx_monitor.SAVE_RESEND_GRACE_SECONDS + 1)
        self.go_idle(m)
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.COMPACT_SENT)

    def test_error_is_terminal(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        self.go_idle(m)
        self.clock.advance(ctx_monitor.SAVE_RESEND_GRACE_SECONDS + 1)
        self.go_idle(m)
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ERROR)
        sends_before = len(self.tmux.sent_texts)
        self.write_tap(pct=50)  # way past T1: must still do nothing
        m.tick()
        m.tick()
        self.assertEqual(len(self.tmux.sent_texts), sends_before)
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ERROR)


class EscapeBackstopTest(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.tmux.panes = {"%1"}

    def reach_save_sent_busy(self, m):
        self.write_tap(pct=36)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)

    def test_escape_sent_when_busy_at_t2(self):
        m = self.make_monitor()
        self.reach_save_sent_busy(m)
        self.write_tap(pct=41)
        m.tick()
        self.assertEqual(self.tmux.sent_keys, [("%1", "Escape")])

    def test_escape_respects_30s_grace_and_max_2(self):
        m = self.make_monitor()
        self.reach_save_sent_busy(m)
        self.write_tap(pct=41)
        m.tick()  # Escape 1
        m.tick()  # 0s later: inside grace
        self.assertEqual(len(self.tmux.sent_keys), 1)
        self.clock.advance(31)
        m.tick()  # Escape 2
        self.assertEqual(len(self.tmux.sent_keys), 2)
        self.clock.advance(31)
        m.tick()  # max 2: never a third
        self.assertEqual(len(self.tmux.sent_keys), 2)

    def test_no_escape_below_t2(self):
        m = self.make_monitor()
        self.reach_save_sent_busy(m)
        self.write_tap(pct=39)
        m.tick()
        self.assertEqual(self.tmux.sent_keys, [])

    def test_no_escape_when_not_busy(self):
        m = self.make_monitor()
        self.reach_save_sent_busy(m)
        self.write_tap(pct=41)
        self.tmux.pane_content["%1"] = "idle prompt"
        m.tick()  # one tick only: not idle yet
        self.assertEqual(self.tmux.sent_keys, [])


class CompactSentTest(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.tmux.panes = {"%1"}

    def reach_compact_sent(self, m):
        self.write_tap(pct=36)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()  # ARMED -> SAVE_SENT
        self.write_checkpoint()
        self.tmux.pane_content["%1"] = "idle prompt"
        m.tick()
        m.tick()  # idle x2 -> verified -> COMPACT_SENT
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.COMPACT_SENT)

    def compact_sends(self):
        return [t for t in self.tmux.sent_texts if t[1] == "/compact"]

    def test_rearm_sends_reorient_and_rearms(self):
        m = self.make_monitor()
        self.reach_compact_sent(m)
        self.write_tap(pct=8)  # compact finished, fresh low pct
        m.tick()
        st = m.panes["%1"]
        self.assertEqual(st["state"], ctx_monitor.ARMED)
        pane, text = self.tmux.sent_texts[-1]
        self.assertEqual(pane, "%1")
        self.assertIn("reorient", text)
        self.assertIn(ctx_monitor.checkpoint_path(self.work_dir, SESSION), text)

    def test_full_cycle_can_trigger_again(self):
        m = self.make_monitor()
        self.reach_compact_sent(m)
        self.write_tap(pct=8)
        m.tick()  # cycle 1 complete, re-armed
        saves_after_cycle1 = len(
            [
                t
                for t in self.tmux.sent_texts
                if "save your full execution state" in t[1]
            ]
        )
        self.clock.advance(5)
        self.write_tap(pct=36)  # climbs past T1 again
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        saves_after_cycle2 = len(
            [
                t
                for t in self.tmux.sent_texts
                if "save your full execution state" in t[1]
            ]
        )
        self.assertEqual(saves_after_cycle2, saves_after_cycle1 + 1)

    def test_above_rearm_before_timeout_waits(self):
        m = self.make_monitor()
        self.reach_compact_sent(m)
        self.write_tap(pct=30)  # above REARM
        self.clock.advance(60)  # < 5 min
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.COMPACT_SENT)
        self.assertEqual(len(self.compact_sends()), 1)

    def test_timeout_retries_compact_once_when_idle(self):
        m = self.make_monitor()
        self.reach_compact_sent(m)
        self.write_tap(pct=30)
        self.clock.advance(301)
        m.tick()
        self.assertEqual(len(self.compact_sends()), 2)
        self.assertTrue(m.panes["%1"]["compact_retried"])
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.COMPACT_SENT)

    def test_timeout_waits_for_idle_before_retry(self):
        m = self.make_monitor()
        self.reach_compact_sent(m)
        self.write_tap(pct=30)
        self.tmux.pane_content["%1"] = self.busy
        self.clock.advance(301)
        m.tick()  # busy at timeout: no retry yet
        self.assertEqual(len(self.compact_sends()), 1)
        self.tmux.pane_content["%1"] = "idle prompt"
        m.tick()
        m.tick()  # idle again -> retry fires
        self.assertEqual(len(self.compact_sends()), 2)

    def test_second_timeout_goes_error(self):
        m = self.make_monitor()
        self.reach_compact_sent(m)
        self.write_tap(pct=30)
        self.clock.advance(301)
        m.tick()  # retry
        self.clock.advance(301)
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ERROR)


class CopyModeDeferralTest(MonitorTestCase):
    """A pane in tmux copy-mode (a human scrolled it) EATS send-keys: Claude
    Code never sees the text. Observed live: a save message sent into
    copy-mode left zero transcript trace, was classified DEAD twice, and the
    pane wrongly escalated to terminal ERROR. Every send site must treat the
    action as NOT-taken (no state mutation) and retry once the mode exits."""

    def setUp(self):
        super().setUp()
        self.tmux.panes = {"%1"}

    def reach_save_sent_busy(self, m):
        self.write_tap(pct=36)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)

    def reach_compact_sent(self, m):
        self.reach_save_sent_busy(m)
        self.write_checkpoint()
        self.tmux.pane_content["%1"] = "idle prompt"
        m.tick()
        m.tick()  # idle x2 -> verified -> COMPACT_SENT
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.COMPACT_SENT)

    def save_sends(self):
        return [
            t for t in self.tmux.sent_texts if "save your full execution state" in t[1]
        ]

    def compact_sends(self):
        return [t for t in self.tmux.sent_texts if t[1] == "/compact"]

    def mode_log_lines(self):
        with open(os.path.join(self.state_dir, "monitor.log")) as f:
            return [l for l in f if "copy-mode" in l]

    def test_armed_send_deferred_state_untouched_then_fires_on_mode_exit(self):
        m = self.make_monitor()
        self.write_tap(pct=36)
        self.tmux.in_mode_panes.add("%1")
        m.tick()
        st = m.panes["%1"]
        self.assertEqual(st["state"], ctx_monitor.ARMED)  # stays ARMED
        self.assertEqual(self.tmux.sent_texts, [])  # nothing sent
        self.assertIsNone(st["cycle_started_at"])  # mutated nothing
        self.assertIsNone(st["checkpoint_path"])
        self.assertIsNone(st["last_save_sent_at"])
        # Mode exits: the crossing re-fires naturally on the next tick.
        self.tmux.in_mode_panes.discard("%1")
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.assertEqual(len(self.save_sends()), 1)

    def test_escape_deferred_no_attempt_burned(self):
        m = self.make_monitor()
        self.reach_save_sent_busy(m)
        sent_at_before = m.panes["%1"]["last_save_sent_at"]
        self.write_tap(pct=41)  # past T2, pane busy -> Escape conditions met
        self.tmux.in_mode_panes.add("%1")
        m.tick()
        st = m.panes["%1"]
        self.assertEqual(self.tmux.sent_keys, [])  # no Escape sent
        self.assertEqual(st["escape_attempts"], 0)
        self.assertIsNone(st["last_escape_at"])
        self.assertEqual(st["last_save_sent_at"], sent_at_before)
        self.tmux.in_mode_panes.discard("%1")
        m.tick()
        self.assertEqual(self.tmux.sent_keys, [("%1", "Escape")])

    def test_compact_send_deferred_then_fires_on_mode_exit(self):
        m = self.make_monitor()
        self.reach_save_sent_busy(m)
        self.write_checkpoint()
        self.tmux.pane_content["%1"] = "idle prompt"
        self.tmux.in_mode_panes.add("%1")
        m.tick()
        m.tick()  # idle x2, checkpoint verified — but pane in copy-mode
        st = m.panes["%1"]
        self.assertEqual(st["state"], ctx_monitor.SAVE_SENT)
        self.assertEqual(self.compact_sends(), [])
        self.assertIsNone(st["compact_sent_at"])
        self.tmux.in_mode_panes.discard("%1")
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.COMPACT_SENT)
        self.assertEqual(self.compact_sends(), [("%1", "/compact")])

    def test_dead_save_never_progresses_toward_error_in_copy_mode(self):
        # THE live bug: sends eaten by copy-mode read DEAD (no transcript
        # trace) and the old code resent then ERRORed. In copy-mode the pane
        # must neither resend nor move toward ERROR — ever.
        m = self.make_monitor()
        self.reach_save_sent_busy(m)
        # No checkpoint; the default resolver finds no transcript -> DEAD.
        self.tmux.pane_content["%1"] = "idle prompt"
        self.tmux.in_mode_panes.add("%1")
        for _ in range(6):  # many idle ticks, each past the resend grace
            m.tick()
            self.clock.advance(ctx_monitor.SAVE_RESEND_GRACE_SECONDS + 1)
        st = m.panes["%1"]
        self.assertEqual(st["state"], ctx_monitor.SAVE_SENT)  # never ERROR
        self.assertFalse(st["save_resent"])  # no resend recorded
        self.assertIsNone(st["save_resent_at"])
        self.assertEqual(len(self.save_sends()), 1)  # initial only
        # Mode exits: the resend now fires once, normally.
        self.tmux.in_mode_panes.discard("%1")
        m.tick()
        self.assertTrue(m.panes["%1"]["save_resent"])
        self.assertEqual(len(self.save_sends()), 2)

    def test_reorient_deferred_no_transition_then_fires_on_mode_exit(self):
        m = self.make_monitor()
        self.reach_compact_sent(m)
        self.write_tap(pct=8)  # compact done: reorient is due
        self.tmux.in_mode_panes.add("%1")
        m.tick()
        st = m.panes["%1"]
        self.assertEqual(st["state"], ctx_monitor.COMPACT_SENT)  # no transition
        self.assertNotIn("reorient", self.tmux.sent_texts[-1][1])
        self.tmux.in_mode_panes.discard("%1")
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ARMED)
        self.assertIn("reorient", self.tmux.sent_texts[-1][1])

    def test_compact_retry_deferred_then_fires_on_mode_exit(self):
        m = self.make_monitor()
        self.reach_compact_sent(m)
        self.write_tap(pct=30)  # stuck above REARM
        self.clock.advance(ctx_monitor.COMPACT_TIMEOUT_SECONDS + 1)
        self.tmux.in_mode_panes.add("%1")
        m.tick()
        st = m.panes["%1"]
        self.assertFalse(st["compact_retried"])  # retry not recorded
        self.assertEqual(len(self.compact_sends()), 1)
        self.tmux.in_mode_panes.discard("%1")
        m.tick()
        self.assertTrue(m.panes["%1"]["compact_retried"])
        self.assertEqual(len(self.compact_sends()), 2)

    def test_deferral_logs_once_per_streak_not_per_tick(self):
        m = self.make_monitor()
        self.write_tap(pct=36)
        self.tmux.in_mode_panes.add("%1")
        m.tick()
        m.tick()
        m.tick()  # 3 deferred ticks -> exactly ONE log line
        self.assertEqual(len(self.mode_log_lines()), 1)
        # Mode exits: the save fires and the streak flag resets.
        self.tmux.in_mode_panes.discard("%1")
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.assertEqual(len(self.mode_log_lines()), 1)
        # A SECOND streak (next pending send: /compact) logs ONE more line.
        self.write_checkpoint()
        self.tmux.pane_content["%1"] = "idle prompt"
        self.tmux.in_mode_panes.add("%1")
        m.tick()
        m.tick()
        m.tick()
        self.assertEqual(len(self.mode_log_lines()), 2)


class RestartResumeTest(MonitorTestCase):
    def test_restart_mid_cycle_resumes_without_double_send(self):
        self.tmux.panes = {"%1"}
        m1 = self.make_monitor()
        self.write_tap(pct=36)
        self.tmux.pane_content["%1"] = self.busy
        m1.tick()
        self.assertEqual(m1.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        sends = len(self.tmux.sent_texts)
        # Simulated restart: a fresh Monitor over the same state dir.
        m2 = self.make_monitor()
        self.assertEqual(m2.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.write_tap(pct=37)  # still above T1: edge trigger holds
        m2.tick()
        self.assertEqual(len(self.tmux.sent_texts), sends)

    def test_restart_resets_error_to_armed(self):
        # ERROR is terminal only for the life of the process that declared
        # it; a restarted monitor gives the pane a fresh chance.
        m1 = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        m1.panes["%1"] = st
        m1._transition("%1", st, ctx_monitor.ERROR, "test fixture")
        m2 = self.make_monitor()
        self.assertEqual(m2.panes["%1"]["state"], ctx_monitor.ARMED)
        self.assertEqual(m2.panes["%1"]["session_id"], SESSION)
        log = "\n".join(m2.recent)
        self.assertIn("reset to ARMED", log)

    def test_restart_in_reorient_sent_rearms_without_sending(self):
        # Crash window: between the reorient send and the immediate re-arm.
        m1 = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        m1.panes["%1"] = st
        m1._transition("%1", st, ctx_monitor.REORIENT_SENT, "test fixture")
        self.tmux.panes = {"%1"}
        m2 = self.make_monitor()
        self.write_tap(pct=10)
        m2.tick()
        self.assertEqual(m2.panes["%1"]["state"], ctx_monitor.ARMED)
        self.assertEqual(self.tmux.sent_texts, [])


class SessionChangeTest(MonitorTestCase):
    def setUp(self):
        super().setUp()
        self.tmux.panes = {"%1"}

    def test_new_session_in_same_pane_resets_to_armed(self):
        m = self.make_monitor()
        self.write_tap(session_id="sessAAAAAAAA", pct=36)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.clock.advance(10)
        self.write_tap(session_id="sessBBBBBBBB", pct=5)  # newer ts wins
        m.tick()
        st = m.panes["%1"]
        self.assertEqual(st["session_id"], "sessBBBBBBBB")
        self.assertEqual(st["state"], ctx_monitor.ARMED)

    def test_session_change_unlinks_old_tap_file(self):
        # When a pane's session_id changes (/clear or relaunch) the old
        # session's <old_sid>.json must be unlinked so files don't accumulate
        # for the life of the pane.
        m = self.make_monitor()
        self.write_tap(session_id="sessAAAAAAAA", pct=36)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        old_tap = os.path.join(self.state_dir, "sessAAAAAAAA.json")
        self.assertTrue(os.path.exists(old_tap))
        self.clock.advance(10)
        self.write_tap(session_id="sessBBBBBBBB", pct=5)  # newer ts wins
        m.tick()
        self.assertFalse(os.path.exists(old_tap))
        self.assertTrue(
            os.path.exists(os.path.join(self.state_dir, "sessBBBBBBBB.json"))
        )

    def test_session_change_clears_error(self):
        m = self.make_monitor()
        self.write_tap(session_id="sessAAAAAAAA", pct=36)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()  # ARMED -> SAVE_SENT
        self.tmux.pane_content["%1"] = "idle prompt"
        m.tick()
        m.tick()  # idle x2, missing -> resend
        self.clock.advance(ctx_monitor.SAVE_RESEND_GRACE_SECONDS + 1)
        m.tick()
        m.tick()  # still missing -> ERROR
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ERROR)
        self.clock.advance(10)
        self.write_tap(session_id="sessBBBBBBBB", pct=5)
        m.tick()
        self.assertEqual(m.panes["%1"]["session_id"], "sessBBBBBBBB")
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ARMED)


class PaneDiesMidTickTest(MonitorTestCase):
    """A pane closing between the list_panes() snapshot and a later send-keys
    in the SAME tick makes the real Tmux raise CalledProcessError. That must
    NOT escape tick() and kill the unattended daemon; the failure is isolated
    per-pane and other panes still process."""

    class RaisingTmux(FakeTmux):
        def __init__(self, dying_pane):
            super().__init__()
            self.dying_pane = dying_pane

        def send_text(self, pane, text):
            if pane == self.dying_pane:
                raise subprocess.CalledProcessError(1, ["tmux", "send-keys"])
            super().send_text(pane, text)

        def send_key(self, pane, key):
            if pane == self.dying_pane:
                raise subprocess.CalledProcessError(1, ["tmux", "send-keys"])
            super().send_key(pane, key)

    def test_send_failure_isolated_other_panes_still_process(self):
        tmux = self.RaisingTmux(dying_pane="%1")
        tmux.panes = {"%1", "%2"}
        self.tmux = tmux
        m = self.make_monitor()
        # Both panes cross T1 in the same tick. %1's save send raises.
        self.write_tap(session_id="sessAAAAAAAA", pct=36, pane="%1")
        self.write_tap(session_id="sessBBBBBBBB", pct=36, pane="%2")
        taps = m.tick()  # must NOT raise
        # %1 logged-and-skipped; %2 still got its save message and advanced.
        self.assertEqual(set(taps), {"%1", "%2"})
        sent_panes = {p for p, _ in tmux.sent_texts}
        self.assertIn("%2", sent_panes)
        self.assertEqual(m.panes["%2"]["state"], ctx_monitor.SAVE_SENT)
        log = "\n".join(m.recent)
        self.assertIn("%1: tick error", log)


class SaveMessageStateTest(unittest.TestCase):
    """Unit tests for save_message_state() — the transcript-aware classifier
    that replaces the old idle+missing resend trigger. It scans a Claude Code
    session transcript (JSONL) for the lifecycle of OUR injected save message
    and returns LIVE (in flight: queued or executing -> never resend) or DEAD
    (cancelled, or never landed -> resend justified).

    Event shapes are pinned to the live Claude Code transcript format verified
    on this machine (2.1.x). NOTE vs the original brief: only `enqueue` (and
    `popAll`) carry a `content` field; `remove`/`dequeue` are content-less. So
    a removal is correlated to OUR message by timestamp ordering after our
    matched enqueue, NOT by a content match on the removal event. The single
    positive "it ran" signal is a `queued_command` attachment whose `prompt`
    equals our save text (it co-fires with the dequeue-for-execution `remove`)
    or a `popAll` whose `content` equals our save text.
    """

    SAVE_TEXT = "save your full execution state to /x/.cc-checkpoint-aa.md"

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        # since_ts is epoch; transcript timestamps are ISO8601 UTC. Pick a
        # base wall-clock and emit events at offsets from it so both sides are
        # comparable. since_ts is the epoch of the base instant.
        self.base = "2026-06-10T12:00:00.000Z"
        self.since_ts = ctx_monitor._iso_to_epoch(self.base)

    def _path(self):
        return os.path.join(self.tmpdir.name, "transcript.jsonl")

    def _write(self, events):
        with open(self._path(), "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        return self._path()

    def _iso(self, offset_seconds):
        """ISO8601 UTC timestamp `offset_seconds` after the base instant."""
        return ctx_monitor._epoch_to_iso(self.since_ts + offset_seconds)

    def _enqueue(self, off, content):
        return {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": self._iso(off),
            "sessionId": "s",
            "content": content,
        }

    def _remove(self, off, op="remove"):
        return {
            "type": "queue-operation",
            "operation": op,
            "timestamp": self._iso(off),
            "sessionId": "s",
        }

    def _popall(self, off, content):
        return {
            "type": "queue-operation",
            "operation": "popAll",
            "timestamp": self._iso(off),
            "sessionId": "s",
            "content": content,
        }

    def _attach(self, off, prompt):
        return {
            "type": "attachment",
            "timestamp": self._iso(off),
            "attachment": {
                "type": "queued_command",
                "prompt": prompt,
                "commandMode": "prompt",
            },
        }

    def _assistant(self, off):
        return {"type": "assistant", "timestamp": self._iso(off), "message": {}}

    def _user(self, off, content, prompt_source="queued"):
        return {
            "type": "user",
            "timestamp": self._iso(off),
            "promptSource": prompt_source,
            "message": {"role": "user", "content": content},
        }

    # 1. LIVE-queued: enqueue only, no removal/attachment -> still in queue.
    def test_live_queued_enqueue_only(self):
        p = self._write([self._enqueue(2, self.SAVE_TEXT)])
        self.assertEqual(
            ctx_monitor.save_message_state(p, self.SAVE_TEXT, self.since_ts),
            ctx_monitor.LIVE,
        )

    # 2. LIVE-executing: enqueue + queued_command attachment + assistant turn.
    def test_live_executing_attachment_and_assistant(self):
        p = self._write(
            [
                self._enqueue(2, self.SAVE_TEXT),
                self._remove(5, op="remove"),  # dequeue-for-execution
                self._attach(5, self.SAVE_TEXT),
                self._assistant(6),
            ]
        )
        self.assertEqual(
            ctx_monitor.save_message_state(p, self.SAVE_TEXT, self.since_ts),
            ctx_monitor.LIVE,
        )

    # 2b. LIVE via popAll (queue flushed and run) with matching content.
    def test_live_popall_with_matching_content(self):
        p = self._write(
            [
                self._enqueue(2, self.SAVE_TEXT),
                self._popall(40, self.SAVE_TEXT),
                self._assistant(41),
            ]
        )
        self.assertEqual(
            ctx_monitor.save_message_state(p, self.SAVE_TEXT, self.since_ts),
            ctx_monitor.LIVE,
        )

    # 2c. LIVE via Escape-then-execute: the EXACT live-observed sequence from
    #     the tgate-e2e disposable session. Escape interrupts the running turn,
    #     the queued save is dequeued (content-less) and re-emitted as a `user`
    #     turn whose content == save_text (promptSource "queued"); NO
    #     queued_command attachment is produced on this path. Must read LIVE.
    def test_live_escape_then_execute_user_turn(self):
        p = self._write(
            [
                self._enqueue(2, self.SAVE_TEXT),
                {  # the Escape interrupt marker
                    "type": "user",
                    "timestamp": self._iso(8),
                    "message": {
                        "role": "user",
                        "content": "[Request interrupted by user]",
                    },
                },
                self._remove(8, op="dequeue"),  # content-less dequeue-for-exec
                self._user(8, self.SAVE_TEXT),  # re-emitted as a real user turn
                self._assistant(9),  # agent acts on it
            ]
        )
        self.assertEqual(
            ctx_monitor.save_message_state(p, self.SAVE_TEXT, self.since_ts),
            ctx_monitor.LIVE,
        )

    # 3. DEAD-cancelled: enqueue + remove, NO attachment/assistant/user after
    #    (a genuine queue clear / deletion with no execution).
    def test_dead_cancelled_remove_without_execution(self):
        p = self._write(
            [
                self._enqueue(2, self.SAVE_TEXT),
                self._remove(8, op="remove"),  # cleared, no queued_command attach
            ]
        )
        self.assertEqual(
            ctx_monitor.save_message_state(p, self.SAVE_TEXT, self.since_ts),
            ctx_monitor.DEAD,
        )

    # 4. DEAD-no-trace: transcript has nothing matching since since_ts.
    def test_dead_no_trace(self):
        p = self._write(
            [
                self._enqueue(-50, self.SAVE_TEXT),  # BEFORE since_ts: ignored
                self._enqueue(3, "some OTHER unrelated message"),
                self._remove(4),
            ]
        )
        self.assertEqual(
            ctx_monitor.save_message_state(p, self.SAVE_TEXT, self.since_ts),
            ctx_monitor.DEAD,
        )

    # 5. Timestamp out-of-order safety: events written out of FILE order but
    #    with correct `timestamp` fields are classified correctly. Here the
    #    removal is written FIRST in the file but timestamped AFTER the
    #    execution attachment, so line-order logic would wrongly say DEAD.
    def test_out_of_order_file_lines_keyed_on_timestamp(self):
        p = self._write(
            [
                self._remove(9, op="remove"),  # line 1, ts +9
                self._enqueue(2, self.SAVE_TEXT),  # line 2, ts +2
                self._attach(6, self.SAVE_TEXT),  # line 3, ts +6 -> executed
            ]
        )
        self.assertEqual(
            ctx_monitor.save_message_state(p, self.SAVE_TEXT, self.since_ts),
            ctx_monitor.LIVE,
        )

    # 6. Missing / unreadable transcript -> DEAD (fail-safe), no crash.
    def test_missing_transcript_is_dead(self):
        self.assertEqual(
            ctx_monitor.save_message_state(
                os.path.join(self.tmpdir.name, "nope.jsonl"),
                self.SAVE_TEXT,
                self.since_ts,
            ),
            ctx_monitor.DEAD,
        )

    def test_unreadable_garbage_transcript_is_dead(self):
        p = os.path.join(self.tmpdir.name, "garbage.jsonl")
        with open(p, "w") as f:
            f.write("{not json\n\x00\x00 broken\n")
        self.assertEqual(
            ctx_monitor.save_message_state(p, self.SAVE_TEXT, self.since_ts),
            ctx_monitor.DEAD,
        )

    def test_none_path_is_dead(self):
        self.assertEqual(
            ctx_monitor.save_message_state(None, self.SAVE_TEXT, self.since_ts),
            ctx_monitor.DEAD,
        )


class SaveSentTranscriptGateTest(MonitorTestCase):
    """Wires save_message_state into SAVE_SENT: a LIVE (queued/executing) save
    message is NEVER resent, even across many idle ticks spanning >60s (the
    headline bug). A DEAD save message IS resent once to the now-idle pane."""

    def setUp(self):
        super().setUp()
        self.tmux.panes = {"%1"}
        # Point the monitor's transcript resolver at a temp file we control,
        # keyed by session id (mirrors find_transcript's contract).
        self.transcript = os.path.join(self.state_dir, "transcript.jsonl")
        self._resolver = lambda sid: self.transcript

    def make_monitor(self, **kwargs):
        m = super().make_monitor(**kwargs)
        m.find_transcript = self._resolver
        return m

    def reach_save_sent(self, m):
        self.write_tap(pct=36)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)

    def go_idle(self, m):
        self.tmux.pane_content["%1"] = "idle prompt"
        m.tick()
        m.tick()

    def save_sends(self):
        return [
            t for t in self.tmux.sent_texts if "save your full execution state" in t[1]
        ]

    def _save_text(self, m):
        return ctx_monitor.SAVE_TEMPLATE.format(
            checkpoint=ctx_monitor.checkpoint_path(self.work_dir, SESSION)
        )

    def _write_transcript(self, events):
        with open(self.transcript, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def _iso(self, epoch):
        return ctx_monitor._epoch_to_iso(epoch)

    # 7. Slow checkpoint: LIVE (executing) across many idle ticks spanning
    #    >60s -> still NO resend (the headline bug).
    def test_live_executing_never_resends_across_60s(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        save_text = self._save_text(m)
        # Transcript shows our save enqueued then picked up for execution.
        sent_at = m.panes["%1"]["last_save_sent_at"]
        self._write_transcript(
            [
                {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "timestamp": self._iso(sent_at + 1),
                    "content": save_text,
                },
                {
                    "type": "attachment",
                    "timestamp": self._iso(sent_at + 2),
                    "attachment": {"type": "queued_command", "prompt": save_text},
                },
            ]
        )
        # Many idle ticks across >60s; checkpoint never appears yet (slow write).
        for _ in range(20):
            self.go_idle(m)
            self.clock.advance(10)  # 200s total
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.assertEqual(len(self.save_sends()), 1)  # NEVER resent
        self.assertFalse(m.panes["%1"]["save_resent"])

    def test_live_queued_never_resends(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        save_text = self._save_text(m)
        sent_at = m.panes["%1"]["last_save_sent_at"]
        self._write_transcript(
            [
                {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "timestamp": self._iso(sent_at + 1),
                    "content": save_text,
                }
            ]
        )
        self.go_idle(m)
        self.clock.advance(90)
        self.go_idle(m)
        self.assertEqual(len(self.save_sends()), 1)
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)

    def test_dead_cancelled_resends_once(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        save_text = self._save_text(m)
        sent_at = m.panes["%1"]["last_save_sent_at"]
        # enqueue then removed with NO execution attachment = cancelled.
        self._write_transcript(
            [
                {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "timestamp": self._iso(sent_at + 1),
                    "content": save_text,
                },
                {
                    "type": "queue-operation",
                    "operation": "remove",
                    "timestamp": self._iso(sent_at + 2),
                },
            ]
        )
        self.go_idle(m)
        st = m.panes["%1"]
        self.assertTrue(st["save_resent"])
        self.assertEqual(len(self.save_sends()), 2)

    def test_dead_no_trace_resends_once(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        # Empty transcript: no trace of our message at all -> DEAD.
        self._write_transcript([])
        self.go_idle(m)
        self.assertTrue(m.panes["%1"]["save_resent"])
        self.assertEqual(len(self.save_sends()), 2)

    # 8. After a resend, if still DEAD next eval -> ERROR (no infinite resend).
    def test_dead_after_resend_goes_error(self):
        m = self.make_monitor()
        self.reach_save_sent(m)
        self._write_transcript([])  # DEAD throughout
        self.go_idle(m)  # resend
        self.assertEqual(len(self.save_sends()), 2)
        self.clock.advance(ctx_monitor.SAVE_RESEND_GRACE_SECONDS + 1)
        self.go_idle(m)  # still DEAD -> ERROR
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ERROR)

    def test_since_ts_advances_past_resend(self):
        # After a DEAD resend, the next state check must key on events AFTER the
        # resend (last_save_sent_at), so an enqueue from the FRESH resend reads
        # LIVE and we DON'T wrongly ERROR.
        m = self.make_monitor()
        self.reach_save_sent(m)
        self._write_transcript([])  # initial: DEAD -> resend
        self.go_idle(m)
        self.assertEqual(len(self.save_sends()), 2)
        st = m.panes["%1"]
        resent_at = st["last_save_sent_at"]
        save_text = self._save_text(m)
        # The resend landed and is now executing.
        self._write_transcript(
            [
                {
                    "type": "queue-operation",
                    "operation": "enqueue",
                    "timestamp": self._iso(resent_at + 1),
                    "content": save_text,
                },
                {
                    "type": "attachment",
                    "timestamp": self._iso(resent_at + 2),
                    "attachment": {"type": "queued_command", "prompt": save_text},
                },
            ]
        )
        self.clock.advance(ctx_monitor.SAVE_RESEND_GRACE_SECONDS + 1)
        self.go_idle(m)
        # LIVE now -> must NOT go ERROR, must NOT resend again.
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.assertEqual(len(self.save_sends()), 2)

    def test_escape_then_no_trace_resends_against_post_escape_events(self):
        # Fail-safe edge: IF an Escape leaves NO trace of the message executing
        # (a genuine queue clear), since_ts moves to the Escape time and a
        # still-DEAD pane resends the save to the now-idle pane.
        m = self.make_monitor()
        self.reach_save_sent(m)
        # Drive busy past T2 so an Escape fires.
        self.write_tap(pct=41)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        self.assertEqual(self.tmux.sent_keys, [("%1", "Escape")])
        escape_at = m.panes["%1"]["last_escape_at"]
        self.assertIsNotNone(escape_at)
        # No execution trace after the Escape.
        self._write_transcript([])
        # Pane now idle below T2.
        self.write_tap(pct=38)
        self.go_idle(m)
        self.assertTrue(m.panes["%1"]["save_resent"])
        self.assertEqual(len(self.save_sends()), 2)

    def test_escape_then_execute_does_not_resend(self):
        # LIVE FINDING (tgate-e2e): Escape on a busy pane with a queued save
        # INTERRUPTS the turn and then EXECUTES the queued save — it re-emits as
        # a `user` turn whose content == save_text (NO queued_command
        # attachment). The gate must read LIVE and NOT resend.
        m = self.make_monitor()
        self.reach_save_sent(m)
        save_text = self._save_text(m)
        # Drive busy past T2 so an Escape fires.
        self.write_tap(pct=41)
        self.tmux.pane_content["%1"] = self.busy
        m.tick()
        self.assertEqual(self.tmux.sent_keys, [("%1", "Escape")])
        escape_at = m.panes["%1"]["last_escape_at"]
        # The exact live sequence after Escape: interrupt marker, content-less
        # dequeue, then the save re-emitted as an executing user turn.
        self._write_transcript(
            [
                {
                    "type": "user",
                    "timestamp": self._iso(escape_at + 0.1),
                    "message": {
                        "role": "user",
                        "content": "[Request interrupted by user]",
                    },
                },
                {
                    "type": "queue-operation",
                    "operation": "dequeue",
                    "timestamp": self._iso(escape_at + 0.1),
                },
                {
                    "type": "user",
                    "timestamp": self._iso(escape_at + 0.2),
                    "promptSource": "queued",
                    "message": {"role": "user", "content": save_text},
                },
                {"type": "assistant", "timestamp": self._iso(escape_at + 1)},
            ]
        )
        # Pane idle below T2; checkpoint not yet written (slow).
        self.write_tap(pct=38)
        for _ in range(5):
            self.go_idle(m)
            self.clock.advance(20)  # 100s — well past the old 60s grace
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)
        self.assertEqual(len(self.save_sends()), 1)  # NEVER resent
        self.assertFalse(m.panes["%1"]["save_resent"])


class CliTest(unittest.TestCase):
    def test_defaults(self):
        args = ctx_monitor.parse_args([])
        self.assertEqual(args.t1, 35)
        self.assertEqual(args.t2, 40)
        self.assertEqual(args.rearm, 20)
        self.assertEqual(args.tick, 5.0)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.state_dir, "/tmp/claude-ctx")

    def test_overrides(self):
        args = ctx_monitor.parse_args(
            [
                "--t1",
                "5",
                "--t2",
                "8",
                "--rearm",
                "2",
                "--tick",
                "3",
                "--dry-run",
                "--state-dir",
                "/tmp/ctx-test",
            ]
        )
        self.assertEqual(args.t1, 5)
        self.assertEqual(args.t2, 8)
        self.assertEqual(args.rearm, 2)
        self.assertEqual(args.tick, 3.0)
        self.assertTrue(args.dry_run)
        self.assertEqual(args.state_dir, "/tmp/ctx-test")


class RenderTest(MonitorTestCase):
    def test_render_lines_show_pane_pct_state_error_red(self):
        m = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        st["state"] = ctx_monitor.ERROR
        m.panes["%1"] = st
        taps = {
            "%1": {
                "session_id": SESSION,
                "pct": 42,
                "pane": "%1",
                "cwd": self.work_dir,
                "ts": self.clock.now,
            }
        }
        lines = m.render_lines(taps)
        rows = [l for l in lines if "%1" in l]
        self.assertEqual(len(rows), 1)
        self.assertIn("ERROR", rows[0])
        self.assertIn("42%", rows[0])
        self.assertIn(SESSION[:8], rows[0])
        self.assertIn(ctx_monitor.ANSI_RED, rows[0])
        self.assertIn(os.path.basename(self.work_dir), rows[0])

    def _tap(self, pane, session_id=SESSION, pct=22):
        return {
            "session_id": session_id,
            "pct": pct,
            "pane": pane,
            "cwd": self.work_dir,
            "ts": self.clock.now,
        }

    def test_header_has_serial_name_and_context_not_pct(self):
        m = self.make_monitor()
        lines = m.render_lines({})
        header = lines[1]
        self.assertIn("#", header)
        self.assertIn("NAME", header)
        self.assertIn("CONTEXT", header)
        self.assertNotIn("PCT", header)

    def test_rows_carry_serial_numbers_in_sorted_order(self):
        m = self.make_monitor()
        m.panes["%1"] = ctx_monitor.new_pane_state(SESSION)
        m.panes["%2"] = ctx_monitor.new_pane_state("sessZZZZZZZZ")
        taps = {
            "%1": self._tap("%1"),
            "%2": self._tap("%2", session_id="sessZZZZZZZZ"),
        }
        lines = m.render_lines(taps)
        row1 = [l for l in lines if "%1" in l][0]
        row2 = [l for l in lines if "%2" in l][0]
        self.assertTrue(row1.lstrip().startswith("1 "))
        self.assertTrue(row2.lstrip().startswith("2 "))

    def test_name_column_from_pane_title_dir_then_dash_fallback(self):
        m = self.make_monitor()
        m.panes["%1"] = ctx_monitor.new_pane_state(SESSION)
        m.panes["%2"] = ctx_monitor.new_pane_state("sessZZZZZZZZ")
        m.panes["%3"] = ctx_monitor.new_pane_state("sessYYYYYYYY")
        # NAME comes from the (already-cleaned) pane title. %1 has a long title
        # (truncated to 18); %2 has no title -> falls back to its DIR basename
        # ("repo"); %3 has no title AND no cwd -> last-resort "-".
        self.tmux.names = {"%1": "checkpoint-rollback-extra-long", "%2": ""}
        taps = {
            "%1": self._tap("%1"),
            "%2": self._tap("%2", session_id="sessZZZZZZZZ"),
            "%3": self._tap("%3", session_id="sessYYYYYYYY"),
        }
        taps["%3"]["cwd"] = ""  # no repo -> no DIR fallback either
        lines = m.render_lines(taps)
        row1 = [l for l in lines if "%1" in l][0]
        row2 = [l for l in lines if "%2" in l][0]
        row3 = [l for l in lines if "%3" in l][0]
        self.assertIn("checkpoint-rollbac", row1)  # 18-char truncation
        self.assertNotIn("checkpoint-rollback-extra-long", row1)
        self.assertIn(os.path.basename(self.work_dir), row2)  # DIR fallback
        self.assertIn(" - ", row3)  # no title + no cwd shows "-"

    def test_state_column_shows_descriptive_labels(self):
        m = self.make_monitor()
        for pane, state in (
            ("%1", ctx_monitor.ARMED),
            ("%2", ctx_monitor.SAVE_SENT),
            ("%3", ctx_monitor.COMPACT_SENT),
            ("%4", ctx_monitor.REORIENT_SENT),
            ("%5", ctx_monitor.ERROR),
        ):
            st = ctx_monitor.new_pane_state(SESSION)
            st["state"] = state
            m.panes[pane] = st
        taps = {p: self._tap(p) for p in m.panes}
        lines = m.render_lines(taps)

        def row(pane):
            return [l for l in lines if pane in l][0]

        self.assertIn("watching", row("%1"))
        self.assertIn("checkpoint requested", row("%2"))
        self.assertIn("compacting", row("%3"))
        self.assertIn("reorienting", row("%4"))
        self.assertIn("ERROR - needs attention", row("%5"))
        self.assertIn(ctx_monitor.ANSI_RED, row("%5"))  # ERROR stays red

    def test_rows_fit_100_columns(self):
        m = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        st["state"] = ctx_monitor.SAVE_SENT  # longest label
        m.panes["%1"] = st
        self.tmux.names = {"%1": "a-very-long-agent-identity-name"}
        lines = m.render_lines({"%1": self._tap("%1")})
        for line in lines[:4]:  # title, header, rule, row
            self.assertLessEqual(len(line), 100, repr(line))


class ResetFlagTest(MonitorTestCase):
    """_handle_reset_flags: the cockpit drops a `reset-<sid>.flag` in state_dir;
    the daemon re-arms the matching ERROR pane (ERROR -> ARMED) and ALWAYS
    consumes the flag, whether it acted or ignored it."""

    def _flag(self, sid):
        path = os.path.join(self.state_dir, "reset-{}.flag".format(sid))
        with open(path, "w") as f:
            f.write("2026-06-13T00:00:00+00:00")
        return path

    def test_reset_flag_clears_error(self):
        m = self.make_monitor()
        m.panes["%1"] = ctx_monitor.new_pane_state("sid-aaaa")
        m.panes["%1"]["state"] = ctx_monitor.ERROR
        path = self._flag("sid-aaaa")
        m._handle_reset_flags()
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ARMED)
        self.assertFalse(os.path.exists(path))

    def test_reset_flag_ignored_when_not_in_error(self):
        m = self.make_monitor()
        m.panes["%2"] = ctx_monitor.new_pane_state("sid-bbbb")  # ARMED default
        path = self._flag("sid-bbbb")
        m._handle_reset_flags()
        self.assertEqual(m.panes["%2"]["state"], ctx_monitor.ARMED)  # unchanged
        self.assertFalse(os.path.exists(path))

    def test_reset_flag_unknown_sid_no_crash(self):
        m = self.make_monitor()
        m.panes["%1"] = ctx_monitor.new_pane_state("sid-aaaa")
        m.panes["%1"]["state"] = ctx_monitor.ERROR
        path = self._flag("sid-zzzz")  # no matching pane
        m._handle_reset_flags()
        self.assertFalse(os.path.exists(path))            # consumed
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.ERROR)  # untouched


class ReArmPersistTest(MonitorTestCase):
    """The disk/memory divergence fix: a restart re-arms ERROR panes in memory
    AND must persist that re-arm so the cockpit's disk-sourced Context tab
    reflects reality immediately (root cause of the '%2 stuck in ERROR' bug).
    Plus: a reset flag for a pane that is ARMED-in-memory but ERROR-on-disk must
    end with the on-disk state synced to a non-ERROR value."""

    def _read_disk_state(self, pane="%1"):
        with open(os.path.join(self.state_dir, "monitor-state.json")) as f:
            return json.load(f)["panes"][pane]["state"]

    def test_load_persists_error_to_armed_rearm(self):
        # Seed disk with a pane saved as ERROR (as a crashed/escalated daemon
        # would have left it).
        m1 = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        m1.panes["%1"] = st
        m1._transition("%1", st, ctx_monitor.ERROR, "seed error on disk")
        self.assertEqual(self._read_disk_state("%1"), ctx_monitor.ERROR)
        # A fresh monitor (= a restart) loads it, re-arms in memory, and MUST
        # persist so disk is ARMED too — not just memory.
        m2 = self.make_monitor()
        self.assertEqual(m2.panes["%1"]["state"], ctx_monitor.ARMED)  # memory
        self.assertEqual(self._read_disk_state("%1"), ctx_monitor.ARMED)  # disk

    def test_reset_flag_syncs_divergent_disk_to_armed(self):
        # Reproduce the live %2 divergence: disk = ERROR, memory = ARMED (as
        # left by a restart whose re-arm did not persist, pre-fix). Even so, a
        # reset flag must drive the on-disk state to a non-ERROR value.
        m = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        m.panes["%1"] = st
        m._transition("%1", st, ctx_monitor.ERROR, "seed disk error")
        # Force the in-memory copy to ARMED WITHOUT persisting (simulate the
        # old, unpersisted restart re-arm), leaving disk at ERROR.
        st["state"] = ctx_monitor.ARMED
        self.assertEqual(self._read_disk_state("%1"), ctx_monitor.ERROR)
        # Drop a reset flag and handle it.
        flag = os.path.join(self.state_dir, "reset-{}.flag".format(SESSION))
        with open(flag, "w") as f:
            f.write("2026-06-13T00:00:00+00:00")
        m._handle_reset_flags()
        self.assertFalse(os.path.exists(flag))                # consumed
        self.assertNotEqual(self._read_disk_state("%1"), ctx_monitor.ERROR)
        self.assertEqual(self._read_disk_state("%1"), ctx_monitor.ARMED)

    def test_reset_flag_does_not_abort_in_flight_cycle(self):
        # Safety property: a reset flag must NOT abort a legitimate in-flight
        # cycle (SAVE_SENT / COMPACT_SENT / REORIENT_SENT) — it only syncs disk.
        m = self.make_monitor()
        st = ctx_monitor.new_pane_state(SESSION)
        m.panes["%1"] = st
        m._transition("%1", st, ctx_monitor.SAVE_SENT, "mid cycle")
        flag = os.path.join(self.state_dir, "reset-{}.flag".format(SESSION))
        with open(flag, "w") as f:
            f.write("2026-06-13T00:00:00+00:00")
        m._handle_reset_flags()
        self.assertFalse(os.path.exists(flag))                       # consumed
        self.assertEqual(m.panes["%1"]["state"], ctx_monitor.SAVE_SENT)  # kept
        self.assertEqual(self._read_disk_state("%1"), ctx_monitor.SAVE_SENT)


if __name__ == "__main__":
    unittest.main()
