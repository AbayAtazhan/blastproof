"""Docker-backed sandbox: one fresh, locked-down, throwaway container per execution.

Defense layers applied to every run (see `DockerSandbox.build_args`):

* no network at all (`--network none`: no interface besides loopback, no DNS,
  no route to the cloud metadata service)
* read-only root filesystem; the dataset is a read-only bind mount; only two
  small size-capped tmpfs mounts are writable (/tmp and /out)
* hard cgroup limits: memory (swap disabled), CPU, number of processes
* every Linux capability dropped, no-new-privileges, Docker's seccomp profile,
  non-root uid 10001
* zero secrets: the container gets an explicit, minimal environment
* optional gVisor runtime (`SANDBOX_RUNTIME=runsc`): the code talks to a
  user-space kernel instead of the host kernel
* soft time limit inside the runner + hard wall-clock kill from the host
* output is read back through a byte cap; flooding it kills the container
* the container is always removed afterwards: nothing persists between tasks
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Sequence

from .config import SandboxLimits, Settings

MARKER = "__BLASTPROOF_RESULT__"
SANDBOX_LABEL = "io.blastproof.sandbox"
STARTUP_GRACE_S = 6  # container start-up time on top of the code's own time limit
ALLOWED_ARTIFACTS = {".png": "image/png", ".csv": "text/csv", ".json": "application/json",
                     ".txt": "text/plain", ".md": "text/markdown"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,95}$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class SandboxUnavailable(RuntimeError):
    """Docker (or the sandbox image) is not usable on this host."""


@dataclass
class Artifact:
    name: str
    mime: str
    size: int
    data: bytes = field(repr=False)


@dataclass
class Containment:
    kind: str
    layer: str
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "layer": self.layer, "detail": self.detail}


@dataclass
class SandboxResult:
    sandbox_id: str
    purpose: str
    runtime: str
    limits: dict
    exit_code: int | None
    duration_ms: int
    exec_seconds: float | None
    timed_out: bool
    oom_killed: bool
    output_flood: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    error: str | None
    error_type: str | None
    peak_rss_kb: int | None
    artifacts: list[Artifact]
    containment: list[Containment]
    envelope_ok: bool

    @property
    def ok(self) -> bool:
        return (self.envelope_ok and self.error is None and not self.timed_out
                and not self.oom_killed and not self.output_flood and self.exit_code == 0)

    def summary(self) -> dict:
        return {
            "sandbox_id": self.sandbox_id,
            "purpose": self.purpose,
            "runtime": self.runtime,
            "limits": self.limits,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "exec_seconds": self.exec_seconds,
            "timed_out": self.timed_out,
            "oom_killed": self.oom_killed,
            "output_flood": self.output_flood,
            "stdout_truncated": self.stdout_truncated,
            "error_type": self.error_type,
            "peak_rss_mb": round(self.peak_rss_kb / 1024, 1) if self.peak_rss_kb else None,
            "containment": [c.to_dict() for c in self.containment],
            "destroyed": True,
        }


# (kind, defense layer, pattern) - matched against the sandbox's error/stderr/stdout.
_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("network_blocked", "--network none (no interface, no DNS, no metadata route)",
     re.compile(r"Network is unreachable|Temporary failure in name resolution|Name or service not known"
                r"|No address associated|Errno 101|Errno -3|Errno -2|Errno 99|Cannot assign requested address"
                r"|Connection refused|getaddrinfo failed", re.I)),
    ("readonly_fs", "read-only root filesystem and read-only dataset mount",
     re.compile(r"Read-only file system|Errno 30")),
    ("disk_quota", "size-capped tmpfs (/tmp, /out)",
     re.compile(r"No space left on device|Errno 28|File too large")),
    ("process_limit", "cgroup pids limit",
     re.compile(r"Resource temporarily unavailable|Errno 11\b|can't start new thread")),
    ("privilege_denied", "all capabilities dropped + no-new-privileges + seccomp",
     re.compile(r"Operation not permitted|Errno 1\b")),
    ("permission_denied", "unprivileged uid 10001",
     re.compile(r"Permission denied|Errno 13\b")),
]


def classify(*, text: str, timed_out: bool, oom: bool, flood: bool, error_type: str | None,
             limits: SandboxLimits, timeout_s: int) -> list[Containment]:
    events: list[Containment] = []
    if timed_out:
        events.append(Containment("timeout", f"wall-clock limit ({timeout_s}s) then hard kill",
                                  f"execution stopped after {timeout_s}s"))
    if oom or error_type == "MemoryError":
        events.append(Containment("memory_limit", f"cgroup memory limit ({limits.memory}, swap disabled)",
                                  "allocation refused / process killed by the OOM killer"))
    if flood:
        events.append(Containment("output_flood", "output byte cap",
                                  "container killed after flooding its output stream"))
    for kind, layer, pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            line = next((ln for ln in text.splitlines() if match.group(0) in ln), match.group(0))
            events.append(Containment(kind, layer, line.strip()[:240]))
    return events


async def _read_capped(stream: asyncio.StreamReader, cap: int, flood: asyncio.Event | None):
    chunks, size, truncated = [], 0, False
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if size < cap:
            take = chunk[: cap - size]
            chunks.append(take)
            size += len(take)
            if len(take) < len(chunk):
                truncated = True
        else:
            truncated = True
        if truncated and flood is not None and not flood.is_set():
            flood.set()
    return b"".join(chunks), truncated


class DockerSandbox:
    def __init__(self, settings: Settings):
        self.docker = settings.docker_bin
        self.image = settings.sandbox_image
        self.runtime = settings.sandbox_runtime
        self.runtime_label = settings.runtime_label
        self.limits = settings.limits
        self._sem = asyncio.Semaphore(settings.max_concurrent_sandboxes)

    # ---- container spec -------------------------------------------------
    def build_args(self, name: str, mounts: Sequence[tuple[Path, str]]) -> list[str]:
        lim = self.limits
        args = [
            self.docker, "run", "-i", "--name", name,
            "--label", f"{SANDBOX_LABEL}=1",
            "--network", "none",
            "--read-only",
            "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={lim.tmp_mb}m,mode=1777",
            "--tmpfs", f"/out:rw,noexec,nosuid,nodev,size={lim.out_mb}m,mode=1777",
            "--memory", lim.memory, "--memory-swap", lim.memory,
            "--cpus", str(lim.cpus),
            "--pids-limit", str(lim.pids),
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--user", "10001:10001",
            "--ulimit", "nofile=256:256",
            "--hostname", "sandbox",
            "--log-driver", "none",
            # Explicit, minimal environment. Nothing from the host leaks in.
            "--env", "HOME=/tmp", "--env", "MPLCONFIGDIR=/tmp", "--env", "MPLBACKEND=Agg",
        ]
        if self.runtime:
            args += ["--runtime", self.runtime]
        for host_path, ctr_path in mounts:
            host = str(Path(host_path).resolve())
            if "," in host or "," in ctr_path:
                raise ValueError("mount paths must not contain commas")
            args += ["--mount", f"type=bind,src={host},dst={ctr_path},readonly"]
        args.append(self.image)
        return args

    # ---- execution --------------------------------------------------------
    async def run(self, code: str, *, purpose: str, mounts: Sequence[tuple[Path, str]] = (),
                  timeout_s: int | None = None,
                  on_start: Callable[[str], Awaitable[None]] | None = None) -> SandboxResult:
        lim = self.limits
        timeout_s = timeout_s or lim.timeout_s
        sandbox_id = "bp-" + uuid.uuid4().hex[:12]
        nonce = secrets.token_hex(12)
        payload = json.dumps({
            "code": code, "nonce": nonce, "timeout": timeout_s,
            "max_stdout": lim.max_stdout_chars, "max_artifact_bytes": lim.max_artifact_bytes,
        }).encode()
        args = self.build_args(sandbox_id, mounts)

        async with self._sem:
            if on_start:
                await on_start(sandbox_id)
            started = time.monotonic()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            except FileNotFoundError as exc:
                raise SandboxUnavailable(f"docker CLI not found ({self.docker})") from exc

            flood = asyncio.Event()
            out_task = asyncio.create_task(_read_capped(proc.stdout, lim.max_stream_bytes, flood))
            err_task = asyncio.create_task(_read_capped(proc.stderr, 256_000, None))
            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass

            killed_for: str | None = None
            wait_task = asyncio.create_task(proc.wait())
            flood_task = asyncio.create_task(flood.wait())
            done, _ = await asyncio.wait({wait_task, flood_task}, timeout=timeout_s + STARTUP_GRACE_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            if wait_task not in done:
                killed_for = "flood" if flood.is_set() else "timeout"
                await self._docker("kill", sandbox_id)
                try:
                    await asyncio.wait_for(asyncio.shield(wait_task), 15)
                except asyncio.TimeoutError:
                    proc.kill()
                    await wait_task
            flood_task.cancel()
            stdout_b, _ = await out_task
            stderr_b, _ = await err_task
            duration_ms = int((time.monotonic() - started) * 1000)
            state = await self._inspect(sandbox_id)
            await self._docker("rm", "-f", "-v", sandbox_id)

        exit_code = state.get("ExitCode", proc.returncode)
        stderr_txt = stderr_b.decode("utf-8", "replace")
        if exit_code == 125 and MARKER not in stdout_b.decode("utf-8", "replace"):
            raise SandboxUnavailable(stderr_txt.strip()[-600:] or "docker run failed (exit 125)")

        return self._build_result(
            sandbox_id=sandbox_id, purpose=purpose, nonce=nonce, timeout_s=timeout_s,
            stdout_b=stdout_b, stderr_txt=stderr_txt, exit_code=exit_code, state=state,
            killed_for=killed_for, duration_ms=duration_ms)

    def _build_result(self, *, sandbox_id, purpose, nonce, timeout_s, stdout_b, stderr_txt,
                      exit_code, state, killed_for, duration_ms) -> SandboxResult:
        lim = self.limits
        raw = stdout_b.decode("utf-8", "replace")
        envelope = None
        tag = f"{MARKER}:{nonce}:"
        idx = raw.rfind(tag)
        if idx != -1:
            line = raw[idx + len(tag):].split("\n", 1)[0]
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                envelope = None
            raw_rest = raw[:idx]
        else:
            raw_rest = raw

        flood = killed_for == "flood"
        hard_timeout = killed_for == "timeout"
        oom = bool(state.get("OOMKilled")) or (exit_code == 137 and killed_for is None)

        if envelope is not None:
            stdout = envelope.get("stdout", "")
            # Anything written straight to fd 1 (bypassing sys.stdout) is still shown.
            extra = raw_rest.strip()
            if extra:
                stdout = (stdout + "\n" + extra)[: lim.max_stdout_chars]
            stderr = (envelope.get("stderr", "") + stderr_txt)[-8000:]
            error = envelope.get("error")
            error_type = envelope.get("error_type")
            timed_out = bool(envelope.get("timed_out")) or hard_timeout
            artifacts = self._decode_artifacts(envelope.get("artifacts", []))
            exec_seconds = envelope.get("exec_seconds")
            peak = envelope.get("peak_rss_kb")
            truncated = bool(envelope.get("stdout_truncated"))
        else:
            stdout = raw_rest[: lim.max_stdout_chars]
            stderr = stderr_txt[-8000:]
            timed_out = hard_timeout
            artifacts, exec_seconds, peak, truncated = [], None, None, len(raw_rest) > lim.max_stdout_chars
            if hard_timeout:
                error_type, error = "TimeoutError", f"Killed: exceeded the hard {timeout_s}s wall-clock limit"
            elif oom:
                error_type, error = "MemoryError", f"Killed: out of memory (limit {lim.memory})"
            elif flood:
                error_type, error = "OutputFlood", "Killed: output stream exceeded the byte cap"
            else:
                error_type = "SandboxCrash"
                error = f"Sandbox process exited with code {exit_code} without returning a result"

        text = "\n".join(filter(None, [error, stderr, stdout]))
        containment = classify(text=text, timed_out=timed_out, oom=oom, flood=flood,
                               error_type=error_type, limits=lim, timeout_s=timeout_s)
        return SandboxResult(
            sandbox_id=sandbox_id, purpose=purpose, runtime=self.runtime_label,
            limits=lim.public() | {"timeout_s": timeout_s}, exit_code=exit_code,
            duration_ms=duration_ms, exec_seconds=exec_seconds, timed_out=timed_out,
            oom_killed=oom, output_flood=flood, stdout=stdout, stderr=stderr,
            stdout_truncated=truncated, error=error, error_type=error_type, peak_rss_kb=peak,
            artifacts=artifacts, containment=containment, envelope_ok=envelope is not None)

    def _decode_artifacts(self, items: list) -> list[Artifact]:
        out, total = [], 0
        for item in items[:12]:
            try:
                name = str(item["name"])
                ext = Path(name).suffix.lower()
                if not _SAFE_NAME.match(name) or ext not in ALLOWED_ARTIFACTS:
                    continue
                data = base64.b64decode(item["b64"], validate=True)
                if ext == ".png" and not data.startswith(_PNG_MAGIC):
                    continue
                total += len(data)
                if total > self.limits.max_artifact_bytes:
                    break
                out.append(Artifact(name=name, mime=ALLOWED_ARTIFACTS[ext], size=len(data), data=data))
            except (KeyError, ValueError, TypeError):
                continue
        return out

    # ---- docker helpers ---------------------------------------------------
    async def _docker(self, *args: str, timeout: float = 20) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.docker, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            return 127, "", "docker not found"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "", "timeout"
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    async def _inspect(self, name: str) -> dict:
        code, out, _ = await self._docker("inspect", "--format", "{{json .State}}", name)
        if code != 0:
            return {}
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {}

    async def ping(self) -> dict:
        code, version, err = await self._docker("version", "--format", "{{.Server.Version}}")
        if code != 0:
            return {"docker": False, "error": (err or "docker unavailable").strip()[-300:]}
        img_code, _, _ = await self._docker("image", "inspect", self.image)
        info = {"docker": True, "docker_version": version.strip(), "image": self.image,
                "image_present": img_code == 0, "runtime": self.runtime_label}
        if self.runtime:
            _, runtimes, _ = await self._docker("info", "--format", "{{json .Runtimes}}")
            info["runtime_installed"] = f'"{self.runtime}"' in runtimes
        return info

    async def reap_orphans(self) -> int:
        """Remove sandbox containers left behind by a crash (lifecycle discipline)."""
        code, out, _ = await self._docker("ps", "-aq", "--filter", f"label={SANDBOX_LABEL}=1")
        ids = out.split() if code == 0 else []
        if ids:
            await self._docker("rm", "-f", "-v", *ids)
        return len(ids)
