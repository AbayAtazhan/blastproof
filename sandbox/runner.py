"""Blastproof in-sandbox runner.

Reads one JSON job from stdin, executes the code, and writes one JSON result
envelope to stdout. This file runs *inside* the isolated container; it is not
a security boundary itself (the container is). Its job is to give the host a
clean, structured result: captured stdout/stderr, the exception if any, and
any files the code saved to /out.
"""

import base64
import contextlib
import io
import json
import os
import resource
import signal
import sys
import time
import traceback

OUT_DIR = "/out"
ALLOWED_EXT = {".png": "image/png", ".csv": "text/csv", ".json": "application/json",
               ".txt": "text/plain", ".md": "text/markdown"}
MARKER = "__BLASTPROOF_RESULT__"


class _CappedBuffer(io.StringIO):
    """StringIO that silently stops growing after `cap` characters."""

    def __init__(self, cap):
        super().__init__()
        self.cap = cap
        self.truncated = False

    def write(self, s):
        room = self.cap - self.tell()
        if room <= 0:
            self.truncated = True
            return len(s)
        if len(s) > room:
            self.truncated = True
            super().write(s[:room])
            return len(s)
        return super().write(s)


class _Timeout(BaseException):
    pass


def _on_alarm(signum, frame):
    raise _Timeout()


def _collect_figures():
    """Save matplotlib figures the code created but forgot to save."""
    if "matplotlib.pyplot" not in sys.modules:
        return
    plt = sys.modules["matplotlib.pyplot"]
    try:
        if any(n.lower().endswith(".png") for n in os.listdir(OUT_DIR)):
            plt.close("all")  # the code saved its own charts; don't duplicate them
            return
        for i, num in enumerate(plt.get_fignums(), start=1):
            fig = plt.figure(num)
            path = os.path.join(OUT_DIR, f"figure_{i}.png")
            if not os.path.exists(path) and fig.axes:
                fig.savefig(path, dpi=144, bbox_inches="tight")
        plt.close("all")
    except Exception:
        pass


def _collect_artifacts(max_total):
    artifacts, total, skipped = [], 0, []
    try:
        names = sorted(os.listdir(OUT_DIR))
    except OSError:
        return artifacts, skipped
    for name in names:
        path = os.path.join(OUT_DIR, name)
        ext = os.path.splitext(name)[1].lower()
        if not os.path.isfile(path) or ext not in ALLOWED_EXT:
            skipped.append(name)
            continue
        size = os.path.getsize(path)
        if total + size > max_total:
            skipped.append(name)
            continue
        with open(path, "rb") as fh:
            data = fh.read()
        total += size
        artifacts.append({"name": name, "mime": ALLOWED_EXT[ext], "size": size,
                          "b64": base64.b64encode(data).decode("ascii")})
    return artifacts, skipped


_ENV_ALLOW = {"PATH", "HOME", "LANG", "HOSTNAME", "MPLBACKEND", "MPLCONFIGDIR",
              "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED"}


def main():
    # The host never passes secrets in, but the base image sets a few build-time
    # variables (e.g. GPG_KEY). Keep only an explicit allowlist.
    for key in list(os.environ):
        if key not in _ENV_ALLOW:
            del os.environ[key]
    job = json.loads(sys.stdin.read())
    code = job["code"]
    nonce = job["nonce"]
    timeout = int(job.get("timeout", 30))
    max_stdout = int(job.get("max_stdout", 20000))
    max_artifacts = int(job.get("max_artifact_bytes", 8_000_000))

    real_stdout = sys.stdout
    out_buf, err_buf = _CappedBuffer(max_stdout), _CappedBuffer(max_stdout // 2)
    error = error_type = None
    timed_out = False

    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(timeout)
    started = time.monotonic()
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            namespace = {"__name__": "__main__"}
            exec(compile(code, "<agent>", "exec"), namespace)
    except _Timeout:
        timed_out = True
        error_type = "TimeoutError"
        error = f"TimeoutError: code exceeded the {timeout}s time limit"
    except SystemExit as exc:
        if exc.code not in (None, 0):
            error_type = "SystemExit"
            error = f"SystemExit: {exc.code}"
    except BaseException as exc:  # noqa: BLE001 - we report everything
        error_type = type(exc).__name__
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        # Drop the runner's own frame so the model sees only its code.
        error = "".join(line for line in tb if "/opt/blastproof/runner.py" not in line)
    finally:
        signal.alarm(0)
    duration = time.monotonic() - started

    _collect_figures()
    artifacts, skipped = _collect_artifacts(max_artifacts)
    usage = resource.getrusage(resource.RUSAGE_SELF)

    result = {
        "stdout": out_buf.getvalue(),
        "stderr": err_buf.getvalue(),
        "stdout_truncated": out_buf.truncated,
        "error": error,
        "error_type": error_type,
        "timed_out": timed_out,
        "exec_seconds": round(duration, 3),
        "peak_rss_kb": usage.ru_maxrss,
        "artifacts": artifacts,
        "skipped_artifacts": skipped,
    }
    real_stdout.write("\n" + MARKER + ":" + nonce + ":" + json.dumps(result) + "\n")
    real_stdout.flush()


if __name__ == "__main__":
    main()
