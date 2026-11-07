"""Benchmark Vultr Inference models on the real agent loop (real sandbox, real data).

.venv/bin/python scripts/bench_models.py glm-5.3 qwen3.8-27b ...
"""

import asyncio
import json
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import Analyst  # noqa: E402
from app.config import ROOT, settings  # noqa: E402
from app.llm import VultrInference  # noqa: E402
from app.sandbox import DockerSandbox  # noqa: E402
from app.store import Store  # noqa: E402

TASKS = [
    ("vendor_invoices.csv", "Find likely duplicate invoices and the total amount at risk"),
    ("saas_revenue.csv", "Chart MRR by plan over time and tell me the growth rate"),
    ("support_tickets.csv", "Which categories breach SLA most often?"),
]


async def bench(model: str) -> list[dict]:
    root = ROOT / "var" / "bench" / model
    shutil.rmtree(root, ignore_errors=True)
    st = replace(settings, data_dir=root, agent_model=model, vision_model="")
    store = Store(root / "bench.db")
    llm = VultrInference(st)
    analyst = Analyst(settings=st, store=store, sandbox=DockerSandbox(st), llm=llm)
    rows = []
    for i, (file, question) in enumerate(TASKS):
        ds_id = f"ds{i}"
        folder = root / "datasets" / ds_id
        folder.mkdir(parents=True)
        shutil.copy(ROOT / "samples" / file, folder / "dataset.csv")
        store.add_dataset(id=ds_id, name=file, filename="dataset.csv", ext=".csv", size=1, sample=True)
        profile, _ = await analyst.profile(store.dataset(ds_id))
        store.set_profile(ds_id, profile, [])
        job = f"job{i}"
        store.create_job(id=job, kind="analysis", dataset_id=ds_id, question=question)
        t0 = time.monotonic()
        await analyst.run(job, store.dataset(ds_id), question)
        events = store.events(job)
        answer = next((e["data"] for e in events if e["type"] == "answer"), {})
        err = next((e["data"]["message"] for e in events if e["type"] == "error"), None)
        meta = store.job(job)["meta"]
        rows.append({"model": model, "task": file.split("_")[0], "status": store.job(job)["status"],
                     "attempts": meta.get("attempts"), "seconds": round(time.monotonic() - t0, 1),
                     "receipts": (answer.get("receipts") or {}).get("score"),
                     "tokens": sum((meta.get("tokens") or {}).values()), "error": err,
                     "answer": (answer.get("markdown") or "")[:400]})
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    await llm.aclose()
    return rows


async def main(models):
    results = await asyncio.gather(*(bench(m) for m in models), return_exceptions=True)
    print("\n==== SUMMARY ====")
    for model, res in zip(models, results):
        if isinstance(res, Exception):
            print(f"{model:32} CRASH {res!r}")
            continue
        ok = sum(r["status"] == "done" for r in res)
        rec = [r["receipts"] for r in res if r["receipts"] is not None]
        print(f"{model:32} done {ok}/{len(res)}  attempts {sum(r['attempts'] or 0 for r in res)}  "
              f"time {sum(r['seconds'] for r in res):.0f}s  receipts {sum(rec)/len(rec) if rec else 0:.2f}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or [settings.agent_model]))
