"""Blastproof HTTP API + static web UI."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agent import Analyst
from .config import ROOT, settings
from .llm import VultrInference
from .sandbox import DockerSandbox, SandboxUnavailable
from .store import Store

ALLOWED_UPLOADS = {".csv", ".tsv", ".txt", ".xlsx", ".xls", ".json", ".parquet"}
SAMPLES_DIR = ROOT / "samples"
WEB_DIR = ROOT / "web"
_ID = re.compile(r"^[a-z0-9-]{4,64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,95}$")

store = Store(settings.data_dir / "blastproof.db")
sandbox = DockerSandbox(settings)
llm = VultrInference(settings)
analyst = Analyst(settings=settings, store=store, sandbox=sandbox, llm=llm)
_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _load_samples() -> None:
    manifest = SAMPLES_DIR / "manifest.json"
    if not manifest.exists():
        return
    for item in json.loads(manifest.read_text()):
        ds_id = "sample-" + item["slug"]
        src = SAMPLES_DIR / item["file"]
        existing = store.dataset(ds_id)
        if existing and existing.get("profile"):
            continue
        ext = src.suffix.lower()
        folder = settings.data_dir / "datasets" / ds_id
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o755)
        dest = folder / f"dataset{ext}"
        shutil.copyfile(src, dest)
        dest.chmod(0o644)
        store.add_dataset(id=ds_id, name=item["name"], filename=dest.name, ext=ext,
                          size=dest.stat().st_size, sample=True)
        try:
            profile, _ = await analyst.profile(store.dataset(ds_id))
            store.set_profile(ds_id, profile, item.get("questions", []))
        except (SandboxUnavailable, ValueError) as exc:
            print(f"[blastproof] could not profile sample {ds_id}: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.mark_interrupted()
    reaped = await sandbox.reap_orphans()
    if reaped:
        print(f"[blastproof] removed {reaped} orphaned sandbox container(s)")
    _spawn(_load_samples())
    yield
    await llm.aclose()


app = FastAPI(title="Blastproof", version="1.0.0", lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"  # revalidate via ETag: new deploys show up at once
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; script-src 'self' https://cdn.jsdelivr.net; connect-src 'self'")
    return response


# ---- rate limiting (public demo URL) -------------------------------------------
_hits: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    if settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(request: Request) -> None:
    now, ip = time.monotonic(), _client_ip(request)
    window = _hits[ip]
    while window and now - window[0] > settings.rate_limit_window_s:
        window.popleft()
    if len(window) >= settings.rate_limit_jobs:
        raise HTTPException(429, f"Rate limit: {settings.rate_limit_jobs} runs per "
                                 f"{settings.rate_limit_window_s // 60} minutes. Try again shortly.")
    window.append(now)


def _check_id(value: str) -> str:
    if not _ID.match(value):
        raise HTTPException(400, "invalid id")
    return value


# ---- health & config ------------------------------------------------------------
@app.get("/api/health")
async def health():
    info = await sandbox.ping()
    return {"ok": info.get("docker") and info.get("image_present"), "sandbox": info,
            "inference": {"configured": llm.configured, "base_url": settings.inference_base_url,
                          "model": settings.agent_model, "vision_model": settings.vision_model or None}}


@app.get("/api/config")
async def config():
    return {"model": settings.agent_model, "vision_model": settings.vision_model or None,
            "runtime": settings.runtime_label, "limits": settings.limits.public(),
            "max_attempts": settings.max_attempts, "max_upload_mb": settings.max_upload_mb,
            "inference_configured": llm.configured,
            "policy": [
                {"layer": "Network", "rule": "--network none", "effect": "no interface, no DNS, no cloud metadata"},
                {"layer": "Secrets", "rule": "explicit env allowlist", "effect": "API keys never enter the sandbox"},
                {"layer": "Filesystem", "rule": "read-only root + read-only dataset",
                 "effect": "only /tmp and /out are writable (tmpfs, size-capped)"},
                {"layer": "Memory", "rule": f"{settings.limits.memory}, swap off", "effect": "OOM kill instead of host pressure"},
                {"layer": "CPU / processes", "rule": f"{settings.limits.cpus} vCPU, {settings.limits.pids} pids",
                 "effect": "runaway processes are capped"},
                {"layer": "Privileges", "rule": "cap-drop ALL, no-new-privileges, seccomp, uid 10001",
                 "effect": "no root, no privilege escalation"},
                {"layer": "Kernel", "rule": settings.runtime_label,
                 "effect": "gVisor user-space kernel when runsc is enabled"},
                {"layer": "Time", "rule": f"{settings.limits.timeout_s}s soft + hard kill", "effect": "nothing runs forever"},
                {"layer": "Lifecycle", "rule": "one container per run, removed after",
                 "effect": "nothing persists between tasks"},
            ]}


@app.get("/api/stats")
async def stats():
    return store.stats()


@app.get("/api/sandboxes")
async def sandboxes(limit: int = 100):
    return store.sandboxes(min(max(limit, 1), 500))


# ---- datasets ----------------------------------------------------------------------
def _public_ds(d: dict) -> dict:
    return {k: d[k] for k in ("id", "name", "ext", "size", "created", "sample", "profile", "suggestions")}


@app.get("/api/datasets")
async def list_datasets():
    return [_public_ds(d) for d in store.datasets()]


@app.get("/api/datasets/{dataset_id}")
async def get_dataset(dataset_id: str):
    ds = store.dataset(_check_id(dataset_id))
    if not ds:
        raise HTTPException(404, "dataset not found")
    return _public_ds(ds) | {"jobs": store.jobs_for_dataset(dataset_id)}


@app.post("/api/datasets")
async def upload_dataset(request: Request, file: UploadFile = File(...)):
    _rate_limit(request)
    original = Path(file.filename or "upload.csv").name
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_UPLOADS:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}. Use: {', '.join(sorted(ALLOWED_UPLOADS))}")
    ds_id = uuid.uuid4().hex[:16]
    folder = settings.data_dir / "datasets" / ds_id
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    dest = folder / f"dataset{ext}"
    limit, size = settings.max_upload_mb * 1024 * 1024, 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > limit:
                fh.close()
                shutil.rmtree(folder, ignore_errors=True)
                raise HTTPException(413, f"File larger than {settings.max_upload_mb} MB")
            fh.write(chunk)
    dest.chmod(0o644)
    name = re.sub(r"[^\w.\- ]", "_", original)[:80]
    store.add_dataset(id=ds_id, name=name, filename=dest.name, ext=ext, size=size, sample=False)
    ds = store.dataset(ds_id)
    try:
        profile, _ = await analyst.profile(ds)
    except SandboxUnavailable as exc:
        store.delete_dataset(ds_id)
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(503, f"Sandbox unavailable: {exc}")
    except (ValueError, json.JSONDecodeError) as exc:
        store.delete_dataset(ds_id)
        shutil.rmtree(folder, ignore_errors=True)
        raise HTTPException(422, str(exc))
    suggestions = await analyst.suggest(ds, profile) if llm.configured else []
    store.set_profile(ds_id, profile, suggestions)
    return _public_ds(store.dataset(ds_id))


# ---- analysis jobs ------------------------------------------------------------------
class AskBody(BaseModel):
    dataset_id: str
    question: str = Field(min_length=3, max_length=1000)


@app.post("/api/jobs")
async def create_job(body: AskBody, request: Request):
    ds = store.dataset(_check_id(body.dataset_id))
    if not ds:
        raise HTTPException(404, "dataset not found")
    if not llm.configured:
        raise HTTPException(503, "Inference is not configured: set VULTR_INFERENCE_API_KEY")
    _rate_limit(request)
    job_id = uuid.uuid4().hex[:16]
    store.create_job(id=job_id, kind="analysis", dataset_id=ds["id"], question=body.question.strip())
    _spawn(analyst.run(job_id, ds, body.question.strip()))
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = store.job(_check_id(job_id))
    if not job:
        raise HTTPException(404, "job not found")
    return job | {"events": store.events(job_id)}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, after: int = 0):
    if not store.job(_check_id(job_id)):
        raise HTTPException(404, "job not found")
    last_id = request.headers.get("last-event-id")
    if last_id and last_id.isdigit():
        after = max(after, int(last_id))

    async def gen():
        async for ev in store.stream(job_id, after):
            if await request.is_disconnected():
                break
            if ev["type"] == "ping":
                yield ": ping\n\n"
                continue
            yield f"id: {ev['seq']}\nevent: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/artifacts/{job_id}/{sandbox_id}/{name}")
async def artifact(job_id: str, sandbox_id: str, name: str):
    _check_id(job_id)
    if not re.match(r"^bp-[a-f0-9]{12}$", sandbox_id) or not _NAME.match(name):
        raise HTTPException(400, "invalid path")
    path = settings.data_dir / "artifacts" / job_id / sandbox_id / name
    if not path.is_file():
        raise HTTPException(404, "artifact not found")
    inline = path.suffix.lower() == ".png"
    return FileResponse(path, media_type="image/png" if inline else "application/octet-stream",
                        filename=None if inline else name,
                        headers={"Cache-Control": "private, max-age=86400"})


@app.exception_handler(SandboxUnavailable)
async def sandbox_down(_: Request, exc: SandboxUnavailable):
    return JSONResponse({"detail": f"Sandbox unavailable: {exc}"}, status_code=503)


# ---- web UI -------------------------------------------------------------------------
@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
