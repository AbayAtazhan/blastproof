"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const EVENT_TYPES = ["job_started", "step", "injection_warning", "code", "sandbox_started", "sandbox_finished",
  "retry", "answer", "chart_check", "error", "job_finished"];
const KIND_LABEL = {
  network_blocked: "Network access blocked", readonly_fs: "Write to read-only filesystem blocked",
  disk_quota: "Disk quota enforced", process_limit: "Process limit enforced",
  privilege_denied: "Privileged operation denied", permission_denied: "Permission denied (non-root)",
  memory_limit: "Memory limit enforced", timeout: "Time limit enforced", output_flood: "Output flood stopped",
};

const state = { config: null, datasets: [], current: null, streams: new Map(), liveSandboxes: new Map() };

// ---------- tiny DOM helper (text is always set via textContent) ----------
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const fill = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
const chip = (text, cls = "") => h("span", { class: `chip ${cls}`, text });
const fmtMs = (ms) => (ms == null ? "-" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`);
const fmtSize = (b) => (b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(0)} KB` : `${(b / 1048576).toFixed(1)} MB`);
const fmtTime = (ts) => new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

function renderMarkdown(md) {
  const div = h("div", { class: "md" });
  if (window.marked && window.DOMPurify) {
    div.innerHTML = DOMPurify.sanitize(marked.parse(md || ""), { USE_PROFILES: { html: true }, FORBID_TAGS: ["img"] });
  } else {
    div.textContent = md || "";
  }
  return div;
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* ignore */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

// ---------- header pills ----------
function renderPills(cfg, health) {
  const pills = $("#pills");
  pills.replaceChildren(
    h("span", { class: "pill" }, h("span", { class: `dot ${cfg.inference_configured ? "" : "warn"}` }), "Vultr Inference ", h("b", { text: cfg.model })),
    h("span", { class: "pill" }, h("span", { class: `dot ${health?.sandbox?.docker ? "" : "bad"}` }), "Sandbox ", h("b", { text: cfg.runtime })),
    h("span", { class: "pill" }, "Network ", h("b", { text: "none" })),
    h("span", { class: "pill" }, "Secrets in sandbox ", h("b", { text: "0" })),
  );
  const banner = $("#banner");
  const problems = [];
  if (!cfg.inference_configured) problems.push("Inference is not configured: set VULTR_INFERENCE_API_KEY in .env and restart.");
  if (health && !health.sandbox?.docker) problems.push("Docker is not reachable, so sandboxes cannot start.");
  else if (health && !health.sandbox?.image_present) problems.push("Sandbox image missing: run `docker build -t blastproof-sandbox sandbox`.");
  banner.textContent = problems.join(" ");
  banner.classList.toggle("hidden", problems.length === 0);
}

// ---------- datasets ----------
async function loadDatasets(selectId) {
  state.datasets = await api("/api/datasets");
  const list = $("#datasets");
  list.replaceChildren(...state.datasets.map((d) => {
    const inj = d.profile?.injection_findings?.length;
    return h("li", {}, h("button", {
      class: `ds-item ${state.current?.id === d.id ? "active" : ""}`, onclick: () => selectDataset(d.id),
    },
    h("div", { class: "ds-name", text: d.name }),
    h("div", { class: "ds-meta" },
      d.profile ? `${d.profile.rows.toLocaleString()} rows · ${d.profile.n_columns} cols` : "profiling…",
      d.sample ? h("span", { class: "tag", text: "demo" }) : null,
      inj ? h("span", { class: "tag warn", text: "injection" }) : null)));
  }));
  const target = selectId || state.current?.id || state.datasets[0]?.id;
  if (target && target !== state.current?.id) await selectDataset(target);
  if (!state.datasets.some((d) => !d.profile)) return;
  setTimeout(() => loadDatasets(), 2500); // samples still being profiled
}

async function selectDataset(id) {
  for (const es of state.streams.values()) es.close();
  state.streams.clear();
  const ds = await api(`/api/datasets/${id}`);
  state.current = ds;
  document.querySelectorAll(".ds-item").forEach((el, i) => el.classList.toggle("active", state.datasets[i]?.id === id));
  renderDatasetHeader(ds);
  renderSuggestions(ds);
  const thread = $("#thread");
  thread.replaceChildren();
  state.liveSandboxes.clear();
  renderLive();
  if (!ds.jobs.length) thread.append($("#tpl-empty").content.cloneNode(true));
  for (const job of ds.jobs) {
    const full = await api(`/api/jobs/${job.id}`);
    const view = mountJob(full.question);
    for (const ev of full.events) view.apply(ev);
    if (full.status === "running") follow(job.id, view, full.events.at(-1)?.seq || 0);
  }
  thread.scrollTop = thread.scrollHeight;
}

function renderDatasetHeader(ds) {
  const p = ds.profile || { rows: 0, n_columns: 0, columns: [] };
  const header = $("#ds-header");
  const cols = h("div", { class: "cols" }, p.columns.slice(0, 24).map((c) =>
    h("span", { class: "col", title: `${c.dtype} · ${c.nulls} nulls · ${c.unique} unique` }, h("b", { text: c.name }), ` ${c.dtype}`)));
  const inj = p.injection_findings || [];
  fill(header, 
    h("div", { class: "ds-title", text: ds.name }),
    h("div", { class: "ds-sub", text: `${p.rows.toLocaleString()} rows · ${p.n_columns} columns · ${fmtSize(ds.size)} · parsed and profiled inside a sandbox` }),
    cols,
    inj.length ? h("div", { class: "callout warn" }, h("span", { class: "icon", text: "⚠" }), h("div", {},
      h("b", { text: `Untrusted data: ${inj.length} cell${inj.length > 1 ? "s look" : " looks"} like a prompt-injection attempt. ` }),
      "The agent treats them as data, never as instructions. ",
      ...inj.slice(0, 1).map((f) => h("span", {}, `Row ${f.row}, column “${f.column}”: `, h("q", { text: f.text }))))) : null,
  );
}

function renderSuggestions(ds) {
  $("#suggestions").replaceChildren(...(ds.suggestions || []).map((q) =>
    h("button", { class: "suggestion", type: "button", text: q, onclick: () => ask(q) })));
}

// ---------- job rendering ----------
function mountJob(question) {
  const thread = $("#thread");
  thread.querySelector(".empty")?.remove();
  const timeline = h("div", { class: "timeline" });
  const foot = h("div", { class: "job-foot hidden" });
  const card = h("article", { class: "job" },
    h("div", { class: "job-q" }, h("span", { class: "who", text: "Q" }), h("span", { text: question })), timeline, foot);
  thread.append(card);
  let pendingStep = null;
  const sandboxEls = new Map();

  const closeStep = () => { if (pendingStep) { pendingStep.classList.add("done"); pendingStep = null; } };
  const scroll = () => { thread.scrollTop = thread.scrollHeight; };

  function apply(ev) {
    const d = ev.data || {};
    switch (ev.type) {
      case "job_started": break;
      case "step": {
        closeStep();
        pendingStep = h("div", { class: "ev ev-step" }, h("span", { class: "spinner" }), h("span", { text: d.title }));
        timeline.append(pendingStep);
        break;
      }
      case "injection_warning": {
        timeline.append(h("div", { class: "callout warn" }, h("span", { class: "icon", text: "⚠" }),
          h("div", {}, h("b", { text: "Prompt-injection text detected in the data. " }),
            "It was handed to the model as quoted, untrusted data with an explicit warning, and the sandbox has no network to act on it anyway.")));
        break;
      }
      case "code": {
        closeStep();
        const risk = d.risk === "high" ? "bad" : d.risk === "medium" ? "warn" : "ok";
        const block = h("details", { class: "block", open: d.attempt === 1 ? true : null },
          h("summary", { class: "block-h" }, h("span", { class: "chev", text: "▸" }),
            h("span", { class: "title", text: d.attempt > 1 ? `Repaired code · attempt ${d.attempt}` : "Plan and code" }),
            h("span", { class: "right" }, chip(`pre-flight: ${d.risk} risk`, risk), chip(`${fmtMs(d.llm_ms)} on Vultr Inference`, "blue"))),
          d.plan?.length ? h("div", { class: "block-b" }, h("ol", { class: "plan" }, d.plan.map((p) => h("li", { text: p })))) : null,
          d.guard?.length ? h("div", { class: "findings" }, d.guard.map((f) =>
            h("div", { class: "finding" }, chip(f.severity, f.severity === "high" ? "bad" : f.severity === "medium" ? "warn" : ""),
              h("span", { text: `${f.message}${f.line ? ` (line ${f.line})` : ""}` })))) : null,
          h("details", { class: "code-wrap" }, h("summary", { class: "block-h" }, h("span", { class: "chev", text: "▸" }),
            h("span", { text: `View code · ${d.code.split("\n").length} lines of Python` })), h("pre", { class: "code", text: d.code })));
        timeline.append(block);
        break;
      }
      case "sandbox_started": {
        closeStep();
        const el = h("div", { class: "block" },
          h("div", { class: "block-h" }, h("span", { class: "spinner" }), h("span", { class: "title", text: "Running in sandbox" }),
            h("span", { class: "chip", text: d.sandbox_id }),
            h("span", { class: "right limits" }, chip(d.runtime), chip("network: none", "ok"), chip("secrets: 0", "ok"),
              chip(`${d.limits.memory} RAM`), chip(`${d.limits.cpus} vCPU`), chip(`${d.limits.pids} pids`), chip(`${d.limits.timeout_s}s`))));
        sandboxEls.set(d.sandbox_id, el);
        timeline.append(el);
        state.liveSandboxes.set(d.sandbox_id, { id: d.sandbox_id, running: true, runtime: d.runtime, started: ev.ts });
        renderLive();
        break;
      }
      case "sandbox_finished": {
        const el = sandboxEls.get(d.sandbox_id) || h("div", { class: "block" });
        if (!sandboxEls.has(d.sandbox_id)) timeline.append(el);
        el.replaceChildren(...sandboxBody(d).childNodes);
        state.liveSandboxes.set(d.sandbox_id, { id: d.sandbox_id, running: false, ok: d.ok, runtime: d.runtime,
          ms: d.duration_ms, blocked: d.containment.length, exit: d.exit_code, mem: d.peak_rss_mb });
        renderLive();
        refreshStats();
        break;
      }
      case "retry": {
        closeStep();
        timeline.append(h("div", { class: "retry" }, h("span", { text: "↻" }), h("span", {}, h("b", { text: "Self-healing: " }), d.reason)));
        break;
      }
      case "answer": {
        closeStep();
        const box = h("div", { class: `answer ${d.status === "failed" ? "failed" : ""}` }, renderMarkdown(d.markdown));
        if (d.receipts) box.append(receiptsRow(d.receipts));
        timeline.append(box);
        break;
      }
      case "chart_check": {
        const answer = timeline.querySelector(".answer .receipts") || timeline.querySelector(".answer");
        answer?.append(chip(`chart checked by ${d.model}: ${d.verdict}${d.note ? ` · ${d.note}` : ""}`, d.verdict === "pass" ? "ok" : "warn"));
        break;
      }
      case "error": {
        closeStep();
        timeline.append(h("div", { class: "error", text: d.message }));
        break;
      }
      case "job_finished": {
        closeStep();
        foot.classList.remove("hidden");
        fill(foot, 
          h("span", { text: `status: ${d.status}` }), h("span", { text: `total ${fmtMs(d.duration_ms)}` }),
          h("span", { text: `${d.sandboxes ?? 0} sandbox${d.sandboxes === 1 ? "" : "es"} (all destroyed)` }),
          h("span", { text: `${d.containment ?? 0} blocked operation${d.containment === 1 ? "" : "s"}` }),
          d.tokens ? h("span", { text: `${(d.tokens.prompt + d.tokens.completion).toLocaleString()} tokens` }) : null);
        setBusy(false);
        break;
      }
      default: break;
    }
    scroll();
  }
  return { apply };
}

function sandboxBody(d) {
  const status = d.ok ? chip("exit 0 · success", "ok") : chip(d.timed_out ? "killed: time limit" : d.oom_killed ? "killed: memory limit"
    : d.output_flood ? "killed: output flood" : `failed · exit ${d.exit_code}`, "bad");
  const wrap = h("div", {},
    h("div", { class: "block-h" },
      h("span", { class: "title", text: d.ok ? "Sandbox run" : "Sandbox run (failed)" }), h("span", { class: "chip", text: d.sandbox_id }),
      h("span", { class: "right" }, status, chip(fmtMs(d.duration_ms)), d.peak_rss_mb ? chip(`${d.peak_rss_mb} MB peak`) : null,
        chip(d.runtime), chip("destroyed ✓", "ok"))));
  if (d.containment?.length) {
    wrap.append(h("div", { class: "blocked" }, d.containment.map((c) =>
      h("div", { class: "blocked-row" }, h("span", { text: "🛡" }),
        h("div", {}, h("div", { class: "k", text: `${KIND_LABEL[c.kind] || c.kind}: ${c.layer}` }), h("div", { class: "d", text: c.detail }))))));
  }
  if (d.stdout) {
    wrap.append(h("details", { open: d.ok ? true : null }, h("summary", { class: "block-h" }, h("span", { class: "chev", text: "▸" }),
      h("span", { text: `stdout${d.stdout_truncated ? " (truncated)" : ""}` })), h("pre", { class: "out", text: d.stdout })));
  }
  if (d.error) {
    wrap.append(h("details", { open: true }, h("summary", { class: "block-h" }, h("span", { class: "chev", text: "▸" }),
      h("span", { text: `error · ${d.error_type || "Error"}` })), h("pre", { class: "out err", text: d.error })));
  }
  if (d.artifacts?.length) {
    wrap.append(h("div", { class: "artifacts" }, d.artifacts.map((a) => a.mime === "image/png"
      ? h("a", { href: a.url, target: "_blank", rel: "noopener" }, h("img", { src: a.url, alt: a.name, loading: "lazy" }))
      : h("a", { class: "dl", href: a.url, download: a.name, text: `⤓ ${a.name} (${fmtSize(a.size)})` }))));
  }
  return wrap;
}

function receiptsRow(r) {
  const row = h("div", { class: "receipts" });
  if (!r.checked) {
    row.append(chip("receipts: no numeric claims to check"));
  } else if (r.matched === r.checked) {
    row.append(chip(`receipts ✓ ${r.matched}/${r.checked} numbers traced to sandbox output`, "ok"));
  } else {
    row.append(chip(`receipts ${r.matched}/${r.checked} numbers traced to sandbox output`, "warn"),
      h("span", { text: `not found in output: ${r.unmatched.join(", ")}` }));
  }
  return row;
}

function follow(jobId, view, after = 0) {
  const es = new EventSource(`/api/jobs/${jobId}/events?after=${after}`);
  state.streams.set(jobId, es);
  for (const type of EVENT_TYPES) {
    es.addEventListener(type, (msg) => {
      const ev = JSON.parse(msg.data);
      view.apply(ev);
      if (type === "job_finished") { es.close(); state.streams.delete(jobId); }
    });
  }
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) state.streams.delete(jobId); };
}

// ---------- asking ----------
function setBusy(busy) {
  $("#send").disabled = busy;
  $("#send").textContent = busy ? "Running…" : "Run in sandbox";
}

async function ask(question) {
  question = (question || "").trim();
  if (!question || !state.current) return;
  setBusy(true);
  $("#question").value = "";
  autoGrow();
  try {
    const { job_id: jobId } = await api("/api/jobs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dataset_id: state.current.id, question }),
    });
    const view = mountJob(question);
    follow(jobId, view);
  } catch (err) {
    setBusy(false);
    const view = mountJob(question);
    view.apply({ type: "error", data: { message: err.message } });
  }
}

function autoGrow() {
  const ta = $("#question");
  ta.style.height = "auto";
  ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`;
}

// ---------- monitor & audit ----------
function renderLive() {
  const live = $("#live");
  const items = [...state.liveSandboxes.values()].reverse();
  if (!items.length) { live.replaceChildren(h("p", { class: "muted", text: "Sandboxes appear here as the agent runs code." })); return; }
  live.replaceChildren(...items.map((s) => h("div", { class: `sbx ${s.running ? "running" : s.blocked ? "blocked" : ""}` },
    h("div", { class: "sbx-h" }, h("span", { class: "sbx-id", text: s.id }),
      s.running ? chip("running", "blue") : s.ok ? chip("ok · destroyed", "ok") : chip("failed · destroyed", "bad")),
    h("div", { class: "sbx-meta" }, h("span", { text: s.runtime }), h("span", { text: "net: none" }),
      s.running ? null : h("span", { text: fmtMs(s.ms) }), s.mem ? h("span", { text: `${s.mem} MB` }) : null,
      s.blocked ? h("span", { text: `${s.blocked} blocked` }) : null))));
}

function statTiles(s) {
  return [
    h("div", { class: "stat" }, h("div", { class: "v", text: s.sandboxes.toLocaleString() }), h("div", { class: "l", text: "sandboxes run" })),
    h("div", { class: "stat" }, h("div", { class: "v", text: s.containment_events.toLocaleString() }), h("div", { class: "l", text: "operations blocked" })),
    h("div", { class: "stat good" }, h("div", { class: "v", text: "0" }), h("div", { class: "l", text: "secrets exposed" })),
    h("div", { class: "stat good" }, h("div", { class: "v", text: "0" }), h("div", { class: "l", text: "sandboxes still alive" })),
  ];
}

async function refreshStats() {
  try {
    const s = await api("/api/stats");
    $("#stats").replaceChildren(...statTiles(s));
    $("#audit-stats").replaceChildren(...statTiles(s),
      h("div", { class: "stat" }, h("div", { class: "v", text: `${s.sandbox_seconds}s` }), h("div", { class: "l", text: "total sandbox time" })),
      h("div", { class: "stat" }, h("div", { class: "v", text: s.analyses.toLocaleString() }), h("div", { class: "l", text: "questions answered" })));
  } catch (_) { /* server restarting */ }
}

async function loadAudit() {
  const rows = await api("/api/sandboxes?limit=200");
  $("#audit-body").replaceChildren(...rows.map((r) => h("tr", {},
    h("td", { class: "mono", text: r.id }), h("td", { text: fmtTime(r.started) }),
    h("td", {}, r.purpose, r.question ? h("div", { class: "muted", text: r.question.slice(0, 70) }) : null),
    h("td", { class: "mono", text: r.runtime }), h("td", { class: "mono", text: fmtMs(r.duration_ms) }),
    h("td", {}, r.ok ? chip(`${r.exit_code}`, "ok") : chip(r.timed_out ? "timeout" : r.oom ? "oom" : r.flood ? "flood" : `${r.exit_code}`, "bad")),
    h("td", {}, h("div", { class: "chips" }, r.guard.length ? r.guard.map((g) => chip(g.rule, g.severity === "high" ? "bad" : "warn")) : chip("clean", "ok"))),
    h("td", {}, h("div", { class: "chips" }, r.containment.length ? r.containment.map((c) => chip(c.kind, "bad")) : h("span", { class: "muted", text: "-" }))),
    h("td", {}, chip("✓", "ok")))));
  if (!rows.length) $("#audit-body").append(h("tr", {}, h("td", { colspan: 9, class: "muted", text: "No sandboxes yet." })));
}

function renderPolicy(cfg) {
  $("#policy-body").replaceChildren(...cfg.policy.map((p) =>
    h("tr", {}, h("td", { text: p.layer }), h("td", { class: "mono", text: p.rule }), h("td", { text: p.effect }))));
}

// ---------- upload ----------
async function upload(file) {
  if (!file) return;
  const zone = $("#dropzone");
  zone.classList.add("busy");
  const label = zone.querySelector("span");
  const old = label.innerHTML;
  label.textContent = `Profiling ${file.name} in a sandbox…`;
  try {
    const body = new FormData();
    body.append("file", file);
    const ds = await api("/api/datasets", { method: "POST", body });
    await loadDatasets(ds.id);
    await selectDataset(ds.id);
  } catch (err) {
    alert(`Upload failed: ${err.message}`);
  } finally {
    zone.classList.remove("busy");
    label.innerHTML = old;
    $("#file").value = "";
  }
}

// ---------- boot ----------
function switchView(view) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  for (const v of ["analyst", "audit", "policy"]) $(`#view-${v}`).classList.toggle("hidden", v !== view);
  if (view === "audit") { loadAudit(); refreshStats(); }
}

async function boot() {
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
  $("#ask").addEventListener("submit", (e) => { e.preventDefault(); ask($("#question").value); });
  $("#question").addEventListener("input", autoGrow);
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask($("#question").value); }
  });
  $("#file").addEventListener("change", (e) => upload(e.target.files[0]));
  const zone = $("#dropzone");
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("drag"); upload(e.dataTransfer.files[0]); });

  const [cfg, health] = await Promise.all([api("/api/config"), api("/api/health").catch(() => null)]);
  state.config = cfg;
  renderPills(cfg, health);
  renderPolicy(cfg);
  refreshStats();
  setInterval(refreshStats, 10000);
  await loadDatasets();
}

boot().catch((err) => {
  const banner = $("#banner");
  banner.textContent = `Could not reach the Blastproof API: ${err.message}`;
  banner.classList.remove("hidden");
});
