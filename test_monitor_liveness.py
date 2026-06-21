"""test_monitor_liveness.py — unit proofs for data.monitor_liveness().

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_monitor_liveness.py

NO real process is queried: subprocess.run is monkeypatched so the test never
depends on a live ctx-monitor daemon. monitor_liveness() now derives liveness
from `pgrep -f ctx-monitor.py` (process truth) instead of the unreliable
/tmp/claude-ctx/monitor.lock file (macOS reaps it after ~3 days and it can hold
a stale dead PID — the same root cause as the duplicate-daemon bug).

  L1. pgrep finds a pid          -> alive=True,  pid=<that pid>.
  L2. pgrep finds MULTIPLE pids  -> alive=True,  pid=<FIRST pid>.
  L3. pgrep finds nothing (rc=1) -> alive=False, pid=None.
  L4. pgrep missing on PATH      -> alive=False, pid=None (FileNotFoundError).
  L5. pgrep times out            -> alive=False, pid=None (TimeoutExpired).
  L6. pgrep argv is correct      -> ["pgrep", "-f", "ctx-monitor.py"], timeout set.
"""

from __future__ import annotations

import subprocess
import sys

from agents_tui import data


class FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def install(result=None, raises=None):
    """Patch subprocess.run for monitor_liveness().

    result: a FakeProc to return. raises: an exception instance to raise instead.
    Returns (calls, restore) where calls records each {argv, kwargs}.
    """
    calls: list[dict] = []
    orig = subprocess.run

    def fake_run(args, **kwargs):
        calls.append({"argv": list(args), "kwargs": kwargs})
        if raises is not None:
            raise raises
        return result if result is not None else FakeProc()

    subprocess.run = fake_run

    def restore():
        subprocess.run = orig

    return calls, restore


def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    print("===== monitor_liveness UNIT TESTS =====")

    # ---- L1: single pid found -> alive=True with that pid ----
    calls, restore = install(FakeProc(returncode=0, stdout="4242\n"))
    try:
        live = data.monitor_liveness()
    finally:
        restore()
    print(f"    L1 alive={live.alive} pid={live.pid}")
    check("L1.single_pid_alive",
          live.alive is True and live.pid == 4242,
          f"(alive={live.alive} pid={live.pid})")

    # ---- L2: multiple pids -> alive=True with the FIRST pid ----
    calls, restore = install(FakeProc(returncode=0, stdout="111\n222\n333\n"))
    try:
        live = data.monitor_liveness()
    finally:
        restore()
    print(f"    L2 alive={live.alive} pid={live.pid}")
    check("L2.multi_pid_takes_first",
          live.alive is True and live.pid == 111,
          f"(alive={live.alive} pid={live.pid})")

    # ---- L3: nothing found (pgrep rc=1, empty stdout) -> not alive ----
    calls, restore = install(FakeProc(returncode=1, stdout=""))
    try:
        live = data.monitor_liveness()
    finally:
        restore()
    print(f"    L3 alive={live.alive} pid={live.pid}")
    check("L3.no_match_dead",
          live.alive is False and live.pid is None,
          f"(alive={live.alive} pid={live.pid})")

    # ---- L4: pgrep missing on PATH (FileNotFoundError) -> not alive ----
    calls, restore = install(raises=FileNotFoundError("no pgrep"))
    try:
        live = data.monitor_liveness()
    finally:
        restore()
    print(f"    L4 alive={live.alive} pid={live.pid}")
    check("L4.pgrep_missing_dead",
          live.alive is False and live.pid is None,
          f"(alive={live.alive} pid={live.pid})")

    # ---- L5: pgrep times out (TimeoutExpired) -> not alive ----
    calls, restore = install(
        raises=subprocess.TimeoutExpired(cmd="pgrep", timeout=2))
    try:
        live = data.monitor_liveness()
    finally:
        restore()
    print(f"    L5 alive={live.alive} pid={live.pid}")
    check("L5.timeout_dead",
          live.alive is False and live.pid is None,
          f"(alive={live.alive} pid={live.pid})")

    # ---- L6: argv + timeout passed to subprocess.run are correct ----
    calls, restore = install(FakeProc(returncode=0, stdout="7\n"))
    try:
        data.monitor_liveness()
    finally:
        restore()
    argv_ok = calls and calls[0]["argv"] == ["pgrep", "-f", "ctx-monitor.py"]
    kw = calls[0]["kwargs"] if calls else {}
    kwargs_ok = (kw.get("capture_output") is True
                 and kw.get("text") is True
                 and isinstance(kw.get("timeout"), (int, float)))
    print(f"    L6 argv={calls[0]['argv'] if calls else None} kwargs={kw}")
    check("L6.pgrep_argv_and_timeout",
          bool(argv_ok) and kwargs_ok,
          f"(argv_ok={bool(argv_ok)} kwargs_ok={kwargs_ok})")

    # ----- report -----
    print("\n===== RESULTS =====")
    for p in passed:
        print("  PASS  ", p)
    for f in failed:
        print("  FAIL  ", f)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
