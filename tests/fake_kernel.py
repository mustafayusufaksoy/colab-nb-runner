"""A stand-in for a Colab kernel, used by the end-to-end tests.

Keeps one globals dict alive and executes each request in it, like a kernel.
`sys.stdout` is a router that sends writes to the request being served, the
way ipykernel's stream routes output to the cell that produced it.

Like ipykernel, the kernel tells the stream about a new request by calling
`sys.stdout.set_parent(...)` on WHATEVER `sys.stdout` is at that moment. If
user code replaced `sys.stdout` with an object without `set_parent`, the real
stream keeps the previous request as its parent and output written to it goes
to a reply nobody is waiting for.
"""
import io
import subprocess
import sys
import traceback
from multiprocessing.connection import Listener


class Router:
    def __init__(self):
        self.current = None

    def set_parent(self, parent):
        self.current = parent

    def write(self, text):
        if self.current is not None:
            self.current.write(text)
        return len(text)

    def flush(self):
        pass


def shell(command):
    r = subprocess.run(command, shell=True, capture_output=True, text=True)
    print(r.stdout + r.stderr, end="")


def translate(code):
    """`!cmd` lines become shell calls; `%magic` lines are dropped."""
    out = []
    for line in code.split("\n"):
        if line.startswith("!"):
            line = f"_fake_shell({line[1:]!r})"
        elif line.startswith("%"):
            line = "pass"
        out.append(line)
    return "\n".join(out)


def main(address):
    router = Router()
    sys.stdout = sys.stderr = router
    scope = {"__name__": "__main__", "_fake_shell": shell}
    with Listener(address, family="AF_UNIX") as listener:
        while True:
            with listener.accept() as conn:
                code = conn.recv()
                if code is None:
                    return
                buf = io.StringIO()
                for stream in (sys.stdout, sys.stderr):
                    if hasattr(stream, "set_parent"):
                        stream.set_parent(buf)
                try:
                    exec(compile(translate(code), "<cell>", "exec"), scope)
                except BaseException:
                    buf.write(traceback.format_exc())
                conn.send(buf.getvalue())


if __name__ == "__main__":
    main(sys.argv[1])
