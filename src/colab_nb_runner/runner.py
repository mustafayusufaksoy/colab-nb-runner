"""Run a notebook cell by cell in a Google Colab CLI session.

The Colab CLI (`google-colab-cli`) executes Python code in a session but has no
"run this .ipynb" command. This module extracts the code cells and runs them in
order in the same kernel, so state carries over between cells like "Run all".

Measured against colab-cli 0.6.0 (16 Sep 2026): the `colab exec` websocket can
drop even on short requests ("Connection was lost"), and after reporting the
error the CLI does not exit, it hangs. Waiting for an hours-long cell inside a
single `exec` is therefore unreliable. Instead:

  - A cell is started in a BACKGROUND THREAD in the kernel; its output goes to
    `/content/_nbrun/<run>-<i>.log` in the VM.
  - State and new log bytes are read with short `exec` polls. A poll only
    reads, so it can be retried after a drop.
  - The launcher is idempotent: before a retry the kernel is asked whether the
    cell already started, so it never starts twice.
  - Every `colab` call has a wall-clock limit; a hung process is killed.

Cells with a line starting with `!` or `%` (IPython magics, e.g. a pip install)
cannot run in a thread. They are sent directly and retried only on a drop, so
they must be safe to run twice.
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DRIVE_MOUNT = r"^drive\.mount\(.*$"
MAGIC = re.compile(r"^\s*[!%]", re.M)
TRACEBACK = "Traceback (most recent call last)"
DROPPED = ("Connection was lost", "Websocket connection is closed",
           "WebSocketConnectionClosedException")
MARK = "@@NBRUN "
CHUNK = 200000

# Seconds. Module-level so tests can shrink them.
PREPARE_TIMEOUT = 30
START_TIMEOUT = 45
POLL_TIMEOUT = 30
KILL_GRACE = 15      # added to a call's own timeout before the process is killed
RETRY_WAIT = 15
MAX_POLL_FAILURES = 40

PREPARE = r'''
import builtins as _nbrun_b, getpass as _nbrun_g, os as _nbrun_os, sys as _nbrun_sys
def _nbrun_no_input(*a, **k):
    raise RuntimeError("colab-nb-runner: input()/getpass() was called "
                       f"{a!r} -- pass the value with a patch instead")
_NBRUN_DIR = __DIR__
_nbrun_os.makedirs(_NBRUN_DIR, exist_ok=True)
import threading as _nbrun_th
if "_NBRUN_LOG" not in globals():
    _NBRUN = {}                          # cell id -> {"state", "start"}
    _NBRUN_LOG = {"file": None}          # log of the cell running right now
    _NBRUN_OUT = _nbrun_sys.stdout       # the kernel's own stream; polls write here
    class _NbrunStream:
        """Stands in for sys.stdout/stderr: writes from cell threads go to the
        running cell's log, everything else to the kernel's stream.

        sys.stdout itself must stay an object the kernel can call set_parent()
        on. MEASURED: when a cell thread swapped sys.stdout for a plain file,
        the kernel could not tell its stream about the next request, so every
        poll came back empty for as long as the cell ran.
        """
        def __init__(self, real):
            self._real = real
        def _target(self):
            f = _NBRUN_LOG["file"]
            if f is not None and _nbrun_th.current_thread() is not _nbrun_th.main_thread():
                return f
            return self._real
        def write(self, text):
            try:
                return self._target().write(text)
            except ValueError:           # log closed between the check and the write
                return self._real.write(text)
        def flush(self):
            try:
                return self._target().flush()
            except ValueError:
                return None
        def __getattr__(self, name):
            return getattr(self._real, name)
    _nbrun_sys.stdout = _NbrunStream(_nbrun_sys.stdout)
    _nbrun_sys.stderr = _NbrunStream(_nbrun_sys.stderr)
print("@@NBRUN ready")
'''

START = r'''
import traceback as _nbrun_tb, time as _nbrun_t
_nbrun_b.input = _nbrun_g.getpass = _nbrun_no_input
def _nbrun_work(no, code):
    f = open(f"{_NBRUN_DIR}/{no}.log", "a", buffering=1, encoding="utf-8")
    _NBRUN_LOG["file"] = f
    try:
        _nbrun_b.input = _nbrun_g.getpass = _nbrun_no_input
        exec(compile(code, f"<{no}>", "exec"), globals())
        _NBRUN[no]["state"] = "done"
    except BaseException as e:
        f.write(_nbrun_tb.format_exc())
        _NBRUN[no]["state"] = "error"
        _hook = globals().get("_nbrun_on_error")
        if _hook:
            try:
                _hook(e)
            except Exception:
                pass
    finally:
        _NBRUN_LOG["file"] = None
        f.close()
if __NO__ in _NBRUN:
    _NBRUN_OUT.write(f"@@NBRUN already {_NBRUN[__NO__]['state']}\n"); _NBRUN_OUT.flush()
else:
    _NBRUN[__NO__] = {"state": "running", "start": _nbrun_t.time()}
    _nbrun_th.Thread(target=_nbrun_work, args=(__NO__, __CODE__), daemon=True).start()
    _NBRUN_OUT.write("@@NBRUN started\n"); _NBRUN_OUT.flush()
'''

POLL = r'''
_nbrun_s = _NBRUN.get(__NO__, {}).get("state", "missing")
try:
    with open(f"{_NBRUN_DIR}/{__NO__}.log", "rb") as _nbrun_f:
        _nbrun_f.seek(__OFS__)
        _nbrun_part = _nbrun_f.read(__CHUNK__)
except FileNotFoundError:
    _nbrun_part = b""
_nbrun_more = len(_nbrun_part) == __CHUNK__
if _nbrun_s == "running" or _nbrun_more:
    # Hand out whole lines only: a chunk that ends mid-line (or mid-character)
    # would be printed broken. The rest comes with the next poll.
    _nbrun_cut = max(_nbrun_part.rfind(b"\n"), _nbrun_part.rfind(b"\r")) + 1
    if _nbrun_cut == 0 and _nbrun_more:      # one line longer than a chunk
        _nbrun_cut = len(_nbrun_part)
        for _nbrun_i in range(3):
            try:
                _nbrun_part[:_nbrun_cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                _nbrun_cut -= 1
    _nbrun_part = _nbrun_part[:_nbrun_cut]
_NBRUN_OUT.write("@@NBRUN begin\n" + _nbrun_part.decode("utf-8", "replace"))
_NBRUN_OUT.write(f"\n@@NBRUN state={_nbrun_s} read={len(_nbrun_part)} "
                 f"more={int(_nbrun_more)}\n")
_NBRUN_OUT.flush()
'''


def code_cells(notebook):
    data = json.loads(Path(notebook).read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in data["cells"] if c["cell_type"] == "code"]


def prepare(sources, patches, skip_patterns):
    """Return (sources, skipped line count). Each patch must match exactly once."""
    for old, new in patches.items():
        n = sum(s.count(old) for s in sources)
        if n != 1:
            raise SystemExit(f"patch matched {n} times (must be exactly 1): {old[:80]!r}")
        sources = [s.replace(old, new) for s in sources]
    skipped = 0
    for pattern in skip_patterns:
        rx = re.compile(pattern, re.M)
        skipped += sum(len(rx.findall(s)) for s in sources)
        sources = [rx.sub(lambda m: "pass  # colab-nb-runner skipped: " + m.group(0), s)
                   for s in sources]
    return sources, skipped


class Colab:
    def __init__(self, session, config=None, binary="colab"):
        self.session, self.config, self.binary = session, config, binary

    def send(self, code, timeout):
        """Return (exit code, output). 124: wall clock exceeded, process killed."""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                         encoding="utf-8") as f:
            f.write(code)
        cmd = [self.binary] + (["--config", self.config] if self.config else []) + [
            "exec", "-s", self.session, "-f", f.name, "--timeout", str(timeout)]
        out_path = f.name + ".out"
        try:
            with open(out_path, "w+", encoding="utf-8") as out:
                p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                     start_new_session=True)
                try:
                    rc = p.wait(timeout=timeout + KILL_GRACE)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
                    rc = 124
                out.seek(0)
                output = out.read()
            return rc, output
        finally:
            os.unlink(f.name)
            if os.path.exists(out_path):
                os.unlink(out_path)

    def resilient(self, code, timeout, tries=4, wait=None):
        """Retry on a drop or a hang. Only for code that is safe to run twice."""
        wait = RETRY_WAIT if wait is None else wait
        for n in range(1, tries + 1):
            rc, output = self.send(code, timeout)
            if not (rc == 124 or any(d in output for d in DROPPED)):
                return rc, output
            print(f"  .. connection problem (try {n}/{tries}, rc {rc}); "
                  f"retrying in {wait} s", flush=True)
            time.sleep(wait)
        return rc, output


def marks(output):
    return [line[len(MARK):] for line in output.splitlines() if line.startswith(MARK)]


def make_ready(colab, log_dir):
    rc, output = colab.resilient(PREPARE.replace("__DIR__", repr(log_dir)),
                                 PREPARE_TIMEOUT)
    if "ready" not in marks(output):
        print(output[-2000:])
        return False
    return True


def run_direct(colab, code, limit):
    """Magic cell: one exec, retried on a drop (the cell must be safe to rerun)."""
    rc, output = colab.resilient(
        "_nbrun_b.input = _nbrun_g.getpass = _nbrun_no_input\n" + code, limit, tries=3)
    sys.stdout.write(output)
    # `colab exec` sometimes exits 0 after a Python exception, so check the text too.
    return rc == 0 and TRACEBACK not in output and not any(d in output for d in DROPPED)


def _poll_code(no, offset):
    return (POLL.replace("__NO__", no).replace("__OFS__", str(offset))
            .replace("__CHUNK__", str(CHUNK)))


def run_background(colab, no, code, limit, interval):
    no = repr(no)                        # "<run>-<cell>" string literal
    start = START.replace("__NO__", no).replace("__CODE__", repr(code))
    for n in range(1, 6):
        rc, output = colab.send(start, START_TIMEOUT)
        if any(m.startswith(("started", "already")) for m in marks(output)):
            break
        # The launcher dropped midway: ASK whether the cell really started.
        rc2, out2 = colab.resilient(_poll_code(no, 0), POLL_TIMEOUT)
        if any(m.startswith("state=") and not m.startswith("state=missing")
               for m in marks(out2)):
            break
        print(f"  .. could not start (try {n}/5), retrying", flush=True)
        time.sleep(RETRY_WAIT)
    else:
        print(output[-2000:])
        return False

    offset, began, failures = 0, time.time(), 0
    while True:
        time.sleep(interval)
        rc, output = colab.send(_poll_code(no, offset), POLL_TIMEOUT)
        state_marks = [m for m in marks(output) if m.startswith("state=")]
        if not state_marks:
            failures += 1
            if failures in (1, 5) or failures % 10 == 0:
                print(f"  .. poll failed ({failures} in a row, rc {rc})", flush=True)
            if failures >= MAX_POLL_FAILURES:
                print("=== POLLING LOST: the cell may still be running in the kernel ===")
                return False
            continue
        failures = 0
        fields = dict(x.split("=", 1) for x in state_marks[-1].split())
        # `colab exec` prints lines of its own; write only the log part.
        body = output.split(MARK + "begin\n", 1)[-1].split("\n" + MARK + "state=")[0]
        read, more = int(fields["read"]), fields.get("more") == "1"
        if read:
            sys.stdout.write(body)
            if not more and fields["state"] != "running" and not body.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        offset += read
        if more:
            continue                     # log piled up; read on without waiting
        if fields["state"] == "done":
            return True
        if fields["state"] in ("error", "missing"):
            return False
        if time.time() - began > limit:
            print(f"=== LIMIT EXCEEDED ({limit:.0f} s): the cell is still running "
                  "in the kernel ===")
            return False


def build_parser():
    ap = argparse.ArgumentParser(
        prog="colab-nb-run",
        description="Run a notebook cell by cell in a Google Colab CLI session.")
    ap.add_argument("notebook")
    ap.add_argument("-s", "--session", help="Colab CLI session name (colab new -s NAME)")
    ap.add_argument("--config", help="Colab CLI session state file (colab --config)")
    ap.add_argument("--colab-bin", default="colab", help="path of the colab executable")
    ap.add_argument("--patch", help='JSON file {"old text": "new text"}; each must '
                    "match exactly once, applied to this run only")
    ap.add_argument("--timeouts", help='JSON file {"code cell index": seconds}')
    ap.add_argument("--default-timeout", type=float, default=1800,
                    help="limit for cells not listed in --timeouts (default 1800 s)")
    ap.add_argument("--poll", type=float, default=20, help="poll interval in seconds")
    ap.add_argument("--start", type=int, default=0,
                    help="skip code cells before this index")
    ap.add_argument("--end", type=int, default=None,
                    help="stop after the cell at this index (inclusive)")
    ap.add_argument("--skip", action="append", default=[], metavar="REGEX",
                    help="replace matching top-level lines with `pass` (repeatable)")
    ap.add_argument("--keep-drive-mount", action="store_true",
                    help="do not skip top-level drive.mount(...) lines")
    ap.add_argument("--log-dir", default="/content/_nbrun",
                    help="directory in the Colab VM for per-cell logs")
    ap.add_argument("--dry-run", action="store_true",
                    help="send nothing; list the prepared cells")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    patches = json.loads(Path(a.patch).read_text(encoding="utf-8")) if a.patch else {}
    timeouts = ({int(k): float(v) for k, v in
                 json.loads(Path(a.timeouts).read_text(encoding="utf-8")).items()}
                if a.timeouts else {})
    skip = ([] if a.keep_drive_mount else [DRIVE_MOUNT]) + a.skip
    sources, skipped = prepare(code_cells(a.notebook), patches, skip)
    last = len(sources) - 1 if a.end is None else a.end
    print(f"notebook: {a.notebook}\n{len(sources)} code cells, {len(patches)} patches, "
          f"{skipped} lines skipped, cells {a.start}..{last}", flush=True)

    if a.dry_run:
        for i, src in enumerate(sources):
            first = next((line for line in src.splitlines() if line.strip()), "")
            mode = "direct" if MAGIC.search(src) else "thread"
            limit = timeouts.get(i, a.default_timeout)
            what = "skip" if not a.start <= i <= last else f"{limit:>6.0f}s {mode}"
            print(f"  {i:2d} {what:15} {len(src):6d} B  {first[:60]}")
        return 0
    if not a.session:
        raise SystemExit("--session is required (except with --dry-run)")

    colab = Colab(a.session, a.config, a.colab_bin)
    # The kernel's _NBRUN record outlives a run. MEASURED: without an id, cell 1
    # of a second run counted as "already done", never started, and the first
    # run's log was read back.
    run_id = time.strftime("%Y%m%d_%H%M%S") + f"_{os.getpid()}"
    print(f"run id: {run_id} (kernel logs {a.log_dir}/{run_id}-*.log)")
    if not make_ready(colab, a.log_dir):
        print("=== PREPARE FAILED ===", flush=True)
        return 1
    began = time.time()
    for i, src in enumerate(sources):
        if not a.start <= i <= last:
            continue
        limit = timeouts.get(i, a.default_timeout)
        magic = bool(MAGIC.search(src))
        print(f"\n=== CELL {i}/{len(sources) - 1} "
              f"({'direct' if magic else 'thread'}, limit {limit:.0f}s) ===", flush=True)
        t0 = time.time()
        ok = (run_direct(colab, src, limit) if magic
              else run_background(colab, f"{run_id}-{i}", src, limit, a.poll))
        print(f"[cell {i}: {time.time() - t0:.1f}s, {'ok' if ok else 'FAILED'}]",
              flush=True)
        if not ok:
            print(f"=== NOTEBOOK STOPPED: cell {i} ===", flush=True)
            return 1
    print(f"\n=== NOTEBOOK DONE ({(time.time() - began) / 60:.1f} min) ===", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
