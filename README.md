# colab-nb-runner

**Run a Jupyter notebook cell by cell in a Google Colab session, from your terminal.** A small wrapper around the [Colab CLI](https://pypi.org/project/google-colab-cli/) for long GPU jobs that must not die with a dropped connection.

*Türkçe özet [aşağıda](#türkçe).*

[![test](https://github.com/mustafayusufaksoy/colab-nb-runner/actions/workflows/test.yml/badge.svg)](https://github.com/mustafayusufaksoy/colab-nb-runner/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/colab-nb-runner.svg)](https://pypi.org/project/colab-nb-runner/)
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## The problem

The Colab CLI can execute Python in a session (`colab exec`), but it has no "run this `.ipynb`" command. Looping over the cells yourself runs into three things. The first two we measured on real runs; the third follows from retrying:

| What happens | What it costs you |
|---|---|
| `colab exec` sometimes exits `0` after a Python exception | a failed notebook looks successful |
| The websocket drops mid-request ("Connection was lost"), and the CLI then hangs instead of exiting | an hours-long cell cannot be awaited in one call |
| Retrying a dropped request can start the same cell twice | a long cell runs twice, or a side effect is duplicated |

## How it works

- Each cell is started in a **background thread in the kernel**. Its output goes to a log file in the VM.
- The runner reads state and new output with **short polls**. A poll only reads, so it is retried freely after a drop.
- Starting a cell is **idempotent**: after a lost reply the runner asks the kernel whether the cell already started before trying again.
- `sys.stdout` stays an object the kernel can talk to; only writes from the cell's threads are routed to the log. Swapping `sys.stdout` for a plain file, the obvious way, makes every poll come back empty while the cell runs (measured, fixed in 0.1.1).
- Every `colab` call has a wall-clock limit; a hung CLI process is killed and the call retried.
- A cell fails on a non-zero exit code **or** a traceback in its output. The run stops at the first failing cell and exits `1`.

Kernel state carries over between cells, as with "Run all".

## Install

```bash
uv tool install google-colab-cli      # the Colab CLI itself, then: colab new -s mysession --gpu T4
pip install colab-nb-runner
```

Python 3.9+, macOS or Linux, no dependencies. Needs the `colab` command on your `PATH`.

## Use

```bash
colab-nb-run notebook.ipynb -s mysession
```

```text
=== CELL 0/0 (thread, limit 1800s) ===
tick 0 12:26:49
tick 1 12:26:56
tick 2 12:27:03
...
tick 8 12:27:45
[cell 0: 68.9s, ok]

=== NOTEBOOK DONE (1.1 min) ===
```

Output arrives while the cell is still running, a few lines per poll.

| Option | Meaning |
|---|---|
| `--dry-run` | Send nothing; list the cells, their limits and how each would run |
| `--timeouts t.json` | `{"3": 5400}`: limit in seconds per **code cell index** (0-based, markdown cells not counted). Others get `--default-timeout` (1800) |
| `--patch p.json` | `{"old text": "new text"}` applied to this run only. Each entry must match **exactly once**, otherwise nothing is sent |
| `--start N`, `--end N` | Run only a range of code cells, e.g. to resume after a failure |
| `--skip REGEX` | Replace matching top-level lines with `pass` (repeatable) |
| `--config FILE` | Colab CLI session state file, passed through as `colab --config` |
| `--poll SECONDS` | Poll interval (20) |

On a Mac, run long jobs under `caffeinate -dimsu colab-nb-run ...`. When the machine sleeps, the session can be lost.

## Things to know

- **Google Drive:** mount it once before the run with `colab drivemount -s mysession`. Top-level `drive.mount(...)` lines in the notebook are skipped by default, because a second mount waits for a browser confirmation nobody will give. Use `--keep-drive-mount` to keep them.
- **`input()` and `getpass()` raise** instead of waiting forever. Pass the value with `--patch`.
- **Magic cells:** a cell with a line starting with `!` or `%` cannot run in a thread. It is sent as one request and retried if the connection drops, so it must be safe to run twice (a `pip install` is).
- **Only text output is shown.** Plots and other rich `display()` output are not captured.
- **A cell over its limit keeps running in the kernel.** A thread cannot be stopped from outside; the runner stops and tells you. Inspect or restart the session.
- **Cells run in a thread,** so code that must be on the main thread (signal handlers, some GUI or async setups) will not work.
- **Error hook:** if the notebook defines `_nbrun_on_error(exception)`, it is called when a threaded cell fails. IPython's own `post_run_cell` hooks do not see these failures.
- Colab Secrets (`userdata.get`) were not readable from `colab exec` when we measured (CLI 0.6.0). Set them from a browser cell in the same session, or with `colab exec --env`.

## Tested

- 20 automated tests run the real runner against a fake kernel and a fake `colab` executable that drops replies, loses requests and hangs on demand. No Colab account needed: `python -m unittest discover -s tests`.
- Checked by hand on a real CPU session with Colab CLI 0.7.4 (11 Nov 2026): state across cells, a magic cell, a failing cell, an `input()` call and live output from a 60-second cell. Not yet re-run on a long GPU job since 0.1.1. The design comes from long GPU pipeline runs with CLI 0.6.0.
- Not tested on Windows; it uses POSIX process groups.

This is not an official Google project.

## License

[MIT](LICENSE)

---

## Türkçe

**Bir Jupyter notebook'unu terminalden, Google Colab oturumunda hücre hücre çalıştırır.** Colab CLI'ın üstüne ince bir katman; bağlantı kopunca ölmemesi gereken uzun GPU işleri için.

Colab CLI Python kodu çalıştırıyor ama `.ipynb` çalıştırmıyor. Hücreleri elle döndürünce üç şeye takıldık: `colab exec` Python hatasında bazen `0` dönüyor, bağlantı kopunca CLI çıkmayıp asılı kalıyor, kopan isteği yeniden denemek aynı hücreyi iki kez başlatabiliyor.

Çözüm: hücre kernel'de arka plan iş parçacığında başlar ve çıktısını VM'deki bir dosyaya yazar. Çalıştırıcı kısa yoklamalarla durumu ve yeni satırları okur; yoklama yalnız okuduğu için kopunca rahatça tekrarlanır. Bir hücreyi yeniden başlatmadan önce kernel'e "zaten başladı mı" diye sorulur. İlk hatalı hücrede durur ve `1` ile çıkar.

```bash
pip install colab-nb-runner
colab-nb-run notebook.ipynb -s oturum
```

Seçenekler ve bilinmesi gerekenler yukarıdaki İngilizce bölümde. Mac'te uzun koşuları `caffeinate -dimsu` altında başlatın.
