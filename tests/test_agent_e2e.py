"""Full agent loop against a real sandbox, with a scripted model standing in for Vultr Inference."""

import asyncio
import shutil
from dataclasses import replace
from pathlib import Path

from app.agent import Analyst, parse_reply
from app.config import ROOT, settings
from app.llm import Completion
from app.sandbox import DockerSandbox
from app.store import Store
from tests.conftest import docker

BROKEN = """PLAN:
- Load the invoices
- Sum amounts by vendor
```python
import pandas as pd
df = pd.read_csv('/data/dataset.csv')
print(df.groupby('vendr')['amount_usd'].sum())
```"""

FIXED = """PLAN:
- Load the invoices
- Sum amounts by vendor and chart the top 5
```python
import pandas as pd
import matplotlib.pyplot as plt
df = pd.read_csv('/data/dataset.csv')
df['amount_usd'] = pd.to_numeric(df['amount_usd'], errors='coerce')
top = df.groupby('vendor')['amount_usd'].sum().sort_values(ascending=False).head(5)
print(top.round(2).to_string())
print('Top vendor spend:', round(top.iloc[0], 2))
top.plot.bar(title='Top vendors by spend')
plt.tight_layout()
plt.savefig('/out/top_vendors.png')
```"""


class ScriptedModel:
    model = "scripted-test-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, messages, **kw):
        self.calls.append([dict(m) for m in messages])
        text = self.replies.pop(0)
        if callable(text):
            text = text(messages)
        return Completion(text=text, model=self.model, prompt_tokens=10, completion_tokens=5, latency_ms=1)


def test_parse_reply():
    plan, code = parse_reply(FIXED)
    assert plan[0] == "Load the invoices" and code.startswith("import pandas")


@docker
def test_agent_self_heals_and_answers_with_receipts():
    # Under the project dir (not /tmp) so Docker Desktop / colima can bind-mount it.
    tmp_path = ROOT / "var" / "test-e2e"
    shutil.rmtree(tmp_path, ignore_errors=True)
    st = replace(settings, data_dir=tmp_path, vision_model="")
    store = Store(tmp_path / "t.db")
    folder = tmp_path / "datasets" / "ds-test"
    folder.mkdir(parents=True)
    shutil.copy(ROOT / "samples" / "vendor_invoices.csv", folder / "dataset.csv")
    folder.chmod(0o755)
    (folder / "dataset.csv").chmod(0o644)
    store.add_dataset(id="ds-test", name="invoices", filename="dataset.csv", ext=".csv", size=1, sample=False)

    def explain(messages):
        stdout = messages[-1]["content"].split("SANDBOX STDOUT (ground truth)\n", 1)[1]
        top = stdout.split("Top vendor spend: ", 1)[1].split()[0]
        return f"**Your top vendor received ${float(top):,.2f}.**\n\n- Spend is concentrated in 5 vendors."

    model = ScriptedModel([BROKEN, FIXED, explain])
    analyst = Analyst(settings=st, store=store, sandbox=DockerSandbox(st), llm=model)

    async def go():
        ds = store.dataset("ds-test")
        profile, _ = await analyst.profile(ds)
        store.set_profile("ds-test", profile, [])
        store.create_job(id="job-test", kind="analysis", dataset_id="ds-test", question="Who are our top vendors?")
        await analyst.run("job-test", store.dataset("ds-test"), "Who are our top vendors?")

    asyncio.run(go())
    events = store.events("job-test")
    types = [e["type"] for e in events]
    assert types[0] == "job_started" and types[-1] == "job_finished"
    assert "injection_warning" in types  # the planted test string in the notes column
    finished = [e["data"] for e in events if e["type"] == "sandbox_finished"]
    assert len(finished) == 2 and not finished[0]["ok"] and finished[1]["ok"]
    assert "KeyError" in finished[0]["error"]
    assert "KeyError" in model.calls[1][-1]["content"]  # the real traceback was fed back
    assert finished[1]["artifacts"][0]["name"] == "top_vendors.png"
    answer = next(e["data"] for e in events if e["type"] == "answer")
    assert answer["receipts"]["checked"] == 1 and answer["receipts"]["matched"] == 1
    job = store.job("job-test")
    assert job["status"] == "done" and job["meta"]["sandboxes"] == 2
