import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from multiprocessing.connection import Client
from pathlib import Path

from colab_nb_runner import runner

HERE = Path(__file__).parent


def notebook(path, *cells):
    data = {"cells": [{"cell_type": "markdown", "source": ["# title"]}] + [
        {"cell_type": "code", "source": c.splitlines(keepends=True)} for c in cells]}
    Path(path).write_text(json.dumps(data), encoding="utf-8")
    return str(path)


class PrepareTest(unittest.TestCase):
    def test_only_code_cells_are_extracted(self):
        with tempfile.TemporaryDirectory() as d:
            nb = notebook(Path(d) / "n.ipynb", "a = 1\nb = 2", "print(a)")
            self.assertEqual(runner.code_cells(nb), ["a = 1\nb = 2", "print(a)"])

    def test_patch_must_match_exactly_once(self):
        sources = ["x = 1", "y = x", "x = 1"]
        with self.assertRaises(SystemExit) as two:
            runner.prepare(sources, {"x = 1": "x = 2"}, [])
        self.assertIn("matched 2 times", str(two.exception))
        with self.assertRaises(SystemExit) as none:
            runner.prepare(sources, {"z = 1": "z = 2"}, [])
        self.assertIn("matched 0 times", str(none.exception))
        self.assertEqual(runner.prepare(sources, {"y = x": "y = 5"}, [])[0][1], "y = 5")

    def test_top_level_drive_mount_is_skipped_but_nested_is_kept(self):
        src = "from google.colab import drive\ndrive.mount('/content/drive')\n" \
              "def again():\n    drive.mount('/content/drive', force_remount=True)\n"
        out, skipped = runner.prepare([src], {}, [runner.DRIVE_MOUNT])
        self.assertEqual(skipped, 1)
        self.assertIn("pass  # colab-nb-runner skipped: drive.mount('/content/drive')", out[0])
        self.assertIn("    drive.mount('/content/drive', force_remount=True)", out[0])
        compile(out[0], "<cell>", "exec")

    def test_dry_run_sends_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            nb = notebook(Path(d) / "n.ipynb", "!pip install x", "y = 1")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = runner.main([nb, "--dry-run", "--colab-bin", "/nonexistent"])
            self.assertEqual(rc, 0)
            self.assertIn("direct", out.getvalue())
            self.assertIn("thread", out.getvalue())


class EndToEndTest(unittest.TestCase):
    """The real runner against a fake kernel and a fake `colab` executable."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="nbr", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.socket = str(self.dir / "k.sock")
        self.kernel = subprocess.Popen([sys.executable, str(HERE / "fake_kernel.py"),
                                        self.socket])
        self.addCleanup(self.stop_kernel)
        for _ in range(100):
            if os.path.exists(self.socket):
                break
            time.sleep(0.05)
        self.faults = self.dir / "faults.json"
        saved = {k: getattr(runner, k) for k in (
            "RETRY_WAIT", "KILL_GRACE", "START_TIMEOUT", "POLL_TIMEOUT")}
        self.addCleanup(lambda: [setattr(runner, k, v) for k, v in saved.items()])
        runner.RETRY_WAIT = 0.05
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ["FAKE_COLAB_SOCKET"] = self.socket
        os.environ.pop("FAKE_COLAB_FAULTS", None)

    def stop_kernel(self):
        try:
            with Client(self.socket, family="AF_UNIX") as conn:
                conn.send(None)
            self.kernel.wait(timeout=5)
        except Exception:
            self.kernel.kill()

    def set_faults(self, faults):
        self.faults.write_text(json.dumps({"calls": 0, "faults": faults}))
        os.environ["FAKE_COLAB_FAULTS"] = str(self.faults)

    def run_nb(self, *cells, extra=()):
        nb = notebook(self.dir / "n.ipynb", *cells)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = runner.main([nb, "-s", "test", "--colab-bin", str(HERE / "bin" / "colab"),
                              "--log-dir", str(self.dir / "logs"), "--poll", "0.05",
                              *extra])
        return rc, out.getvalue()

    def mark(self, name):
        """Cell source that appends one line to a file, to count executions."""
        return f"open({str(self.dir / name)!r}, 'a').write('x\\n')"

    def count(self, name):
        p = self.dir / name
        return len(p.read_text().splitlines()) if p.exists() else 0

    def test_state_carries_over_between_cells(self):
        rc, out = self.run_nb("x = 20", "y = x + 1", "print('answer', y * 2)")
        self.assertEqual(rc, 0, out)
        self.assertIn("answer 42", out)
        self.assertIn("=== NOTEBOOK DONE", out)

    def test_exception_stops_the_run_at_that_cell(self):
        rc, out = self.run_nb(self.mark("a"), "raise ValueError('boom')", self.mark("c"))
        self.assertEqual(rc, 1)
        self.assertIn("ValueError: boom", out)
        self.assertIn("=== NOTEBOOK STOPPED: cell 1 ===", out)
        self.assertEqual((self.count("a"), self.count("c")), (1, 0))

    def test_magic_cell_fails_on_traceback_even_with_exit_code_zero(self):
        rc, out = self.run_nb("!echo installing\nraise RuntimeError('bad install')",
                              self.mark("after"))
        self.assertEqual(rc, 1)
        self.assertIn("installing", out)
        self.assertIn("(direct,", out)
        self.assertEqual(self.count("after"), 0)

    def test_cell_starts_once_when_the_launch_reply_is_lost(self):
        self.set_faults({"2": "drop_after"})          # call 1 is prepare
        rc, out = self.run_nb(self.mark("once") + "\nprint('ran')")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.count("once"), 1)
        self.assertIn("ran", out)

    def test_cell_starts_when_the_launch_never_arrived(self):
        self.set_faults({"2": "drop_before"})
        rc, out = self.run_nb(self.mark("once"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.count("once"), 1)
        self.assertIn("could not start (try 1/5)", out)

    def test_lost_polls_are_retried_without_losing_output(self):
        self.set_faults({"3": "drop_before", "4": "drop_after", "5": "drop_before"})
        rc, out = self.run_nb("import time\nfor i in range(4):\n"
                              "    print('line', i)\n    time.sleep(0.05)")
        self.assertEqual(rc, 0, out)
        self.assertEqual([l for l in out.splitlines() if l.startswith("line ")],
                         ["line 0", "line 1", "line 2", "line 3"])

    def test_hung_colab_process_is_killed_and_the_call_retried(self):
        runner.KILL_GRACE = runner.POLL_TIMEOUT = 0.5
        self.set_faults({"3": "hang"})
        t0 = time.time()
        rc, out = self.run_nb("print('survived')")
        self.assertEqual(rc, 0, out)
        self.assertIn("survived", out)
        self.assertIn("rc 124", out)
        self.assertLess(time.time() - t0, 20)

    def test_output_arrives_while_the_cell_is_still_running(self):
        gate = self.dir / "gate"
        cell = ("import os, time\nprint('first line', flush=True)\n"
                f"while not os.path.exists({str(gate)!r}):\n    time.sleep(0.02)\n"
                "print('released')")
        seen = {}

        class Watch(io.StringIO):
            def write(inner, text):
                if "first line" in text and not seen:
                    seen["live"] = True
                    gate.write_text("go")          # only now may the cell finish
                return super().write(text)

        nb = notebook(self.dir / "n.ipynb", cell)
        timeouts = self.dir / "t.json"
        timeouts.write_text('{"0": 5}')
        out = Watch()
        with contextlib.redirect_stdout(out):
            rc = runner.main([nb, "-s", "test", "--colab-bin", str(HERE / "bin" / "colab"),
                              "--log-dir", str(self.dir / "logs"), "--poll", "0.05",
                              "--timeouts", str(timeouts)])
        self.assertEqual(rc, 0, out.getvalue())
        self.assertTrue(seen.get("live"))
        self.assertNotIn("poll failed", out.getvalue())
        self.assertIn("released", out.getvalue())

    def test_cell_over_its_limit_stops_the_run(self):
        timeouts = self.dir / "t.json"
        timeouts.write_text('{"0": 0.3}')
        rc, out = self.run_nb("import time\ntime.sleep(3)", self.mark("after"),
                              extra=["--timeouts", str(timeouts)])
        self.assertEqual(rc, 1)
        self.assertIn("LIMIT EXCEEDED", out)
        self.assertEqual(self.count("after"), 0)

    def test_input_raises_instead_of_hanging(self):
        rc, out = self.run_nb("name = input('who? ')")
        self.assertEqual(rc, 1)
        self.assertIn("input()/getpass() was called", out)

    def test_second_run_in_the_same_kernel_runs_the_cells_again(self):
        cell = self.mark("twice") + "\nprint('hello')"
        first = self.run_nb(cell)
        time.sleep(1.1)                                # run id has 1 s resolution
        second = self.run_nb(cell)
        self.assertEqual((first[0], second[0]), (0, 0))
        self.assertEqual(self.count("twice"), 2)
        self.assertEqual(second[1].count("hello"), 1)

    def test_large_output_is_read_completely(self):
        saved = runner.CHUNK
        self.addCleanup(setattr, runner, "CHUNK", saved)
        runner.CHUNK = 1000
        rc, out = self.run_nb("for i in range(500):\n    print('satır ığüşöç', i)")
        self.assertEqual(rc, 0, out)
        rows = [l for l in out.splitlines() if l.startswith("satır ")]
        self.assertEqual(rows, [f"satır ığüşöç {i}" for i in range(500)])

    def test_line_longer_than_a_chunk_arrives_intact(self):
        saved = runner.CHUNK
        self.addCleanup(setattr, runner, "CHUNK", saved)
        runner.CHUNK = 1000
        rc, out = self.run_nb("print('ş' * 2500)\nprint('end')")
        self.assertEqual(rc, 0, out)
        self.assertIn("ş" * 2500 + "\nend\n", out)

    def test_colab_cli_noise_is_not_mixed_into_cell_output(self):
        rc, out = self.run_nb("print('clean')")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("[colab] executing", out)

    def test_error_hook_is_called_for_a_failing_cell(self):
        hook = ("def _nbrun_on_error(e):\n"
                f"    open({str(self.dir / 'hook')!r}, 'w').write(type(e).__name__)")
        rc, out = self.run_nb(hook, "1 / 0")
        self.assertEqual(rc, 1)
        self.assertEqual((self.dir / "hook").read_text(), "ZeroDivisionError")

    def test_patch_and_cell_range(self):
        patch = self.dir / "p.json"
        patch.write_text(json.dumps({"N = 1": "N = 7"}))
        rc, out = self.run_nb(self.mark("skipped"), "N = 1", "print('N is', N)",
                              self.mark("late"),
                              extra=["--patch", str(patch), "--start", "1", "--end", "2"])
        self.assertEqual(rc, 0, out)
        self.assertIn("N is 7", out)
        self.assertEqual((self.count("skipped"), self.count("late")), (0, 0))


if __name__ == "__main__":
    unittest.main()
