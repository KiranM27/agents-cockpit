"""test_send.py — unit proofs for data.send_message_to_pane.

Run with the venv python from the agents-cockpit dir:
    .venv/bin/python test_send.py

NO real tmux is invoked: subprocess.run is monkeypatched to a recorder that
returns a configurable fake CompletedProcess. We assert the EXACT tmux command
sequence for each path:

  S1. SINGLE-LINE: copy-mode guard (display-message) -> send-keys -l <text>
      -> send-keys Enter (as a SEPARATE call). Returns (True, "").
  S2. MULTI-LINE: copy-mode guard -> load-buffer - (raw text via stdin)
      -> paste-buffer -p -t <pane> -> send-keys Enter. Returns (True, "").
  S3. COPY-MODE GUARD: when #{pane_in_mode} == "1", returns
      (False, "pane is scrolled ...") and sends NOTHING else.
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


def install_recorder(in_mode_value="0", fail_on=None):
    """Patch subprocess.run with a recorder.

    in_mode_value: what `display-message ... #{pane_in_mode}` returns on stdout.
    fail_on: a substring; if a command's argv contains it, return returncode 1.
    Returns (calls, restore) where calls is a list of {argv, input}.
    """
    calls: list[dict] = []
    orig = subprocess.run

    def fake_run(args, **kwargs):
        calls.append({"argv": list(args), "input": kwargs.get("input")})
        # the copy-mode guard query
        if "display-message" in args:
            return FakeProc(returncode=0, stdout=in_mode_value)
        if fail_on is not None and any(fail_on in a for a in args):
            return FakeProc(returncode=1, stdout="")
        return FakeProc(returncode=0, stdout="")

    subprocess.run = fake_run

    def restore():
        subprocess.run = orig

    return calls, restore


def run() -> int:
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        (passed if cond else failed).append(f"{name}  {detail}".rstrip())

    print("===== send_message_to_pane UNIT TESTS =====")

    # ---- S1: single-line ----
    calls, restore = install_recorder(in_mode_value="0")
    try:
        ok, err = data.send_message_to_pane("%5", "hello world")
    finally:
        restore()
    argvs = [c["argv"] for c in calls]
    s1_guard = argvs[0] == ["tmux", "display-message", "-p", "-t", "%5",
                            "#{pane_in_mode}"]
    s1_literal = ["tmux", "send-keys", "-t", "%5", "-l", "hello world"] in argvs
    s1_enter = ["tmux", "send-keys", "-t", "%5", "Enter"] in argvs
    # literal must come BEFORE Enter, and Enter must be a SEPARATE call
    s1_order = (s1_literal and s1_enter
                and argvs.index(["tmux", "send-keys", "-t", "%5", "-l",
                                 "hello world"])
                < argvs.index(["tmux", "send-keys", "-t", "%5", "Enter"]))
    no_loadbuffer = not any("load-buffer" in a for a in argvs)
    print(f"    S1 ok={ok} err={err!r} ncalls={len(calls)}")
    print(f"       argvs={argvs}")
    check("S1.single_line_sequence",
          ok and err == "" and s1_guard and s1_order and no_loadbuffer,
          f"(ok={ok} guard={s1_guard} order={s1_order})")

    # ---- S2: multi-line ----
    calls, restore = install_recorder(in_mode_value="0")
    text = "first line\nsecond line"
    try:
        ok, err = data.send_message_to_pane("%7", text)
    finally:
        restore()
    argvs = [c["argv"] for c in calls]
    s2_guard = argvs[0] == ["tmux", "display-message", "-p", "-t", "%7",
                            "#{pane_in_mode}"]
    # load-buffer - with the raw text fed via stdin (input=)
    lb_idx = next((i for i, c in enumerate(calls)
                   if c["argv"] == ["tmux", "load-buffer", "-"]), None)
    s2_loadbuffer = lb_idx is not None and calls[lb_idx]["input"] == text
    s2_paste = ["tmux", "paste-buffer", "-p", "-t", "%7"] in argvs
    s2_enter = ["tmux", "send-keys", "-t", "%7", "Enter"] in argvs
    # order: load-buffer -> paste-buffer -> Enter
    s2_order = (s2_loadbuffer and s2_paste and s2_enter
                and lb_idx < argvs.index(["tmux", "paste-buffer", "-p",
                                          "-t", "%7"])
                < argvs.index(["tmux", "send-keys", "-t", "%7", "Enter"]))
    no_literal = not any("-l" in a for a in argvs)
    print(f"    S2 ok={ok} err={err!r} ncalls={len(calls)}")
    print(f"       argvs={argvs}")
    print(f"       load-buffer stdin == raw text: {s2_loadbuffer}")
    check("S2.multi_line_sequence",
          ok and err == "" and s2_guard and s2_order and no_literal,
          f"(ok={ok} guard={s2_guard} order={s2_order} no_literal={no_literal})")

    # ---- S3: copy-mode guard returns False, sends nothing else ----
    calls, restore = install_recorder(in_mode_value="1")
    try:
        ok, err = data.send_message_to_pane("%9", "should not send")
    finally:
        restore()
    argvs = [c["argv"] for c in calls]
    guard_only = (len(calls) == 1
                  and argvs[0] == ["tmux", "display-message", "-p", "-t",
                                   "%9", "#{pane_in_mode}"])
    refused = (ok is False
               and "copy-mode" in err)
    print(f"    S3 ok={ok} err={err!r} ncalls={len(calls)}")
    check("S3.copy_mode_guard_refuses",
          refused and guard_only,
          f"(ok={ok} err={err!r} guard_only={guard_only})")

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
