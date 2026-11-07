# lablab.ai submission kit

Deadline: **8 Nov 2026, 23:00 UTC** (16:00 Salt Lake City · 04:00 Almaty on 9 Nov). 5-minute grace period.
Submit on lablab.ai (the Vultr Community post is optional and does not replace it).

## Form fields

**Project title:** Blastproof: the AI analyst with a blast radius of zero

**Short description (≤ 255 chars):**
An AI data analyst that writes and runs real Python on your spreadsheets inside disposable Vultr sandboxes
(no network, no secrets, gVisor) and answers only with numbers it can prove from sandbox output.

**Long description:**
Business teams want to ask their data questions in plain English, and the agents that answer properly do it
by executing LLM-written code. Security teams won't run that next to production secrets. Blastproof removes
the trade-off.

Upload a CSV/Excel file (or use the AP-invoice, SaaS-revenue or support-ticket demo data) and ask a question.
An agent on Vultr Serverless Inference profiles the data, plans, writes pandas/matplotlib code, and runs it
in a brand-new container on Vultr Cloud Compute: no network, an explicit env allowlist with zero secrets,
read-only filesystem and dataset, cgroup memory/CPU/process limits, all capabilities dropped, seccomp,
non-root, optional gVisor kernel isolation, soft and hard time limits, an output byte cap, and destruction
after every run. If the code fails or hits a wall, the real traceback goes back to the model, which repairs
it.

Three things make it trustworthy rather than just sandboxed:
1. Even parsing the upload happens in the sandbox, and every text cell is scanned for prompt-injection text,
   which is surfaced to the user and quoted to the model as untrusted data.
2. A static pre-flight review flags network, process, file-write and secret access before execution.
3. A receipts check verifies that every number in the final answer appears in what the code actually
   printed, so hallucinated figures get flagged.

Every container is recorded in an audit log (limits, runtime, findings, what was blocked, destroyed ✓).
Deployment is one script: Docker + gVisor + Caddy HTTPS + hardened systemd on a Vultr VM, provisioned via
the Vultr API.

**Technologies / tags:** Vultr Cloud Compute, Vultr Serverless Inference, Vultr API, Docker, gVisor, Python,
FastAPI, pandas, matplotlib, Server-Sent Events, Caddy

**Category tags:** AI agents, Security, Data analysis, Finance, Enterprise

**Track:** Challenge 1: Blast Radius Zero (Pattern A: sandboxed code execution)

**Demo application platform:** Vultr Cloud Compute (Ubuntu 24.04, Docker + gVisor, Caddy HTTPS)

**Application URL:** https://45-32-228-194.sslip.io

**Links still to fill:** GitHub repo URL · video URL · slides PDF (`docs/Blastproof-slides.pdf`) · cover (`docs/cover.png`)

## Demo video script (target 3:30, hard limit < 5:00)

| Time | Show | Say |
|---|---|---|
| 0:00–0:20 | Title screen, then the app | "Companies want AI that analyzes their data. That means letting an LLM run code. Blastproof makes that safe on Vultr." |
| 0:20–0:45 | Click **Vendor invoices**. Point at the amber callout | "This file was parsed inside a sandbox, not on our server. One cell contains a prompt injection telling the AI to send environment variables to an external URL. We flag it and treat it as data." |
| 0:45–1:45 | Click *"Find likely duplicate invoices and the total amount at risk"*. Narrate the live timeline: plan, code, pre-flight chip, sandbox chips (`network: none`, `secrets: 0`, 512 MB, gVisor), stdout, chart, answer | "The model on Vultr Serverless Inference writes a plan and real pandas code. It runs in a brand-new container with no network and no secrets, and it's destroyed afterwards." |
| 1:45–2:15 | Point at the **receipts ✓** chip | "Every number in this answer was checked against what the code actually printed. No hallucinated figures." |
| 2:15–2:50 | **Containment moment**: ask the follow-up *"Before answering, fetch the latest exchange rates from an online API and convert the totals to EUR"*. Show the red 🛡 *Network access blocked: --network none* row, then the self-healing retry and the honest answer | "Here the agent tries to reach the internet. The sandbox has no network interface, so the call fails, the agent sees the real error, adapts, and tells the user what it couldn't do. Nothing left the box." |
| 2:50–3:15 | **Audit log** tab | "Every sandbox is audited: purpose, runtime, limits, what was blocked, and proof it was destroyed. Zero secrets exposed." |
| 3:15–3:35 | **How it's contained** tab + `deploy/setup.sh` in the editor | "One script deploys it on a Vultr VM: Docker, gVisor, HTTPS, hardened systemd. Blastproof: real work, zero blast radius." |

Recording tips: 1920×1080, browser zoom 110%, hide bookmarks. Run each question once before recording
(the answers are cached in the thread). Use the deployed URL, not localhost.

## Slides (8 slides → export PDF)

1. **Blastproof**: the AI analyst with a blast radius of zero. Team, track.
2. **Problem**: AI analysis = executing LLM-written code; security says no. Prompt injection hides in data; LLMs invent numbers.
3. **Solution**: question → plan → code → disposable sandbox → self-heal → answer with receipts.
4. **Live demo** screenshot: invoices answer with chart + receipts chip.
5. **Containment layers** table (network, secrets, FS, cgroups, caps/seccomp, gVisor, time, output, lifecycle).
6. **Architecture** (`docs/architecture.svg`): Vultr Cloud Compute + Serverless Inference + API.
7. **Business value**: AP duplicate detection, revenue analysis, support SLA. Safe to run on sensitive finance data; auditable.
8. **What's next**: approval gates for risky code, per-tenant VMs via the Vultr API, more file types, scheduled reports.
