"""Runtime configuration, read once from the environment (and .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return default if not raw else raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SandboxLimits:
    memory: str = "512m"
    cpus: float = 1.0
    pids: int = 64
    timeout_s: int = 30
    tmp_mb: int = 64
    out_mb: int = 32
    max_stdout_chars: int = 20_000
    max_artifact_bytes: int = 8_000_000
    max_stream_bytes: int = 16_000_000  # hard cap on what we read back from the container

    def public(self) -> dict:
        return {
            "memory": self.memory,
            "swap": "none",
            "cpus": self.cpus,
            "pids": self.pids,
            "timeout_s": self.timeout_s,
            "tmp_mb": self.tmp_mb,
            "out_mb": self.out_mb,
        }


@dataclass(frozen=True)
class Settings:
    inference_base_url: str
    inference_api_key: str
    agent_model: str
    vision_model: str
    sandbox_image: str
    sandbox_runtime: str
    docker_bin: str
    limits: SandboxLimits
    max_attempts: int
    max_concurrent_sandboxes: int
    data_dir: Path
    max_upload_mb: int
    rate_limit_jobs: int
    rate_limit_window_s: int
    daily_job_limit: int
    trust_proxy: bool

    @property
    def runtime_label(self) -> str:
        return "gVisor (runsc)" if self.sandbox_runtime == "runsc" else (self.sandbox_runtime or "runc")


def load_settings() -> Settings:
    data_dir = Path(_str("DATA_DIR", "./var"))
    if not data_dir.is_absolute():
        data_dir = (ROOT / data_dir).resolve()
    return Settings(
        inference_base_url=_str("INFERENCE_BASE_URL", "https://api.vultrinference.com/v1").rstrip("/"),
        inference_api_key=_str("VULTR_INFERENCE_API_KEY"),
        agent_model=_str("AGENT_MODEL", "mimo-v2.6-pro"),
        vision_model=_str("VISION_MODEL", ""),
        sandbox_image=_str("SANDBOX_IMAGE", "blastproof-sandbox:latest"),
        sandbox_runtime=_str("SANDBOX_RUNTIME", ""),
        docker_bin=_str("DOCKER_BIN", "docker"),
        limits=SandboxLimits(
            memory=_str("SANDBOX_MEMORY", "512m"),
            cpus=_float("SANDBOX_CPUS", 1.0),
            pids=_int("SANDBOX_PIDS", 64),
            timeout_s=_int("SANDBOX_TIMEOUT", 30),
        ),
        max_attempts=_int("MAX_ATTEMPTS", 3),
        max_concurrent_sandboxes=_int("MAX_CONCURRENT_SANDBOXES", 4),
        data_dir=data_dir,
        max_upload_mb=_int("MAX_UPLOAD_MB", 20),
        rate_limit_jobs=_int("RATE_LIMIT_JOBS", 12),
        rate_limit_window_s=_int("RATE_LIMIT_WINDOW_S", 600),
        daily_job_limit=_int("DAILY_JOB_LIMIT", 150),
        trust_proxy=_bool("TRUST_PROXY", False),
    )


settings = load_settings()
