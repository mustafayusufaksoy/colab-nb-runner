"""A stand-in for a Colab kernel, used by the end-to-end tests.

Keeps one globals dict alive and executes each request in it, like a kernel.
`sys.stdout` is a router that sends writes to the request being served, the
way ipykernel's stream routes output to the cell that produced it.
"""
import io
import subprocess
import sys
import traceback
from multiprocessing.connection import Listener


class Router:
    def __init__(self):
        self.current = None

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
                buf = router.current = io.StringIO()
                try:
                    exec(compile(translate(code), "<cell>", "exec"), scope)
                except BaseException:
                    buf.write(traceback.format_exc())
                router.current = None
                conn.send(buf.getvalue())


if __name__ == "__main__":
    main(sys.argv[1])
