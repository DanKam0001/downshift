"use strict";
(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (x) => (x == null ? "–" : (Math.round(x * 1000) / 10).toString().replace(/\.0$/, "") + "%");
  const usd = (x, d = 4) => (x == null ? "–" : "US$" + Number(x).toFixed(d));
  const ms = (x) => (x == null ? "–" : x < 1000 ? Math.round(x) + " ms" : (x / 1000).toFixed(2) + " s");

  let presets = [], selected = null, tiers = [], es = null, health = null;
  let S = null; // per-run view state

  function freshState() {
    return { rows: {}, bar: null, trainBar: null, rounds: {}, verdict: null, cap: 0.1, spend: 0, graders: new Set(), currentTier: null, runId: null, ref: null, rowPts: null };
  }

  /* ---------- theme ---------- */
  $("#theme-btn").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("ds-theme", root.dataset.theme); } catch (e) {}
  });

  /* ---------- tabs ---------- */
  const tabs = [$("#tab-preset"), $("#tab-custom")];
  function selectTab(t) {
    tabs.forEach((b) => {
      const on = b === t;
      b.setAttribute("aria-selected", on);
      b.tabIndex = on ? 0 : -1;
      document.getElementById(b.getAttribute("aria-controls")).hidden = !on;
    });
    updateRunBtn();
  }
  tabs.forEach((b, i) => {
    b.addEventListener("click", () => selectTab(b));
    b.addEventListener("keydown", (e) => {
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") { const n = tabs[(i + 1) % 2]; selectTab(n); n.focus(); }
    });
  });
  const customMode = () => $("#tab-custom").getAttribute("aria-selected") === "true";

  /* ---------- grader badge ---------- */
  function setGraderBadge(kind) {
    const b = $("#grader-badge"), dot = b.querySelector(".dot"), txt = b.querySelector("span:last-child");
    if (kind === "sandbox") { dot.className = "dot ok"; txt.textContent = "Graded in Nebius Sandboxes"; }
    else if (kind === "local") { dot.className = "dot warn"; txt.textContent = "Graded locally (Sandboxes unavailable)"; }
    else { dot.className = "dot"; txt.textContent = "Graded in Nebius Sandboxes"; }
  }

  /* ---------- presets ---------- */
  function renderPresets() {
    const box = $("#presets");
    if (!presets.length) { box.innerHTML = '<p class="muted">No preset tasks found.</p>'; return; }
    box.innerHTML = presets.map((p, i) => `
      <button type="button" class="preset" role="radio" aria-checked="${selected === p.slug}" tabindex="${(selected ? selected === p.slug : i === 0) ? 0 : -1}" data-slug="${esc(p.slug)}">
        <span class="title">${esc(p.title)}</span>
        <p class="story">${esc(p.business_story)}</p>
        <span class="meta">${p.n_train} tuning rows, ${p.n_holdout} held out${p.license ? " · " + esc(p.license) : ""}${p.source === "fixture" ? " · dev fixture" : ""}</span>
      </button>`).join("");
    const btns = [...box.querySelectorAll(".preset")];
    btns.forEach((b, i) => {
      b.addEventListener("click", () => { selected = b.dataset.slug; renderPresets(); box.querySelector(`[data-slug="${CSS.escape(selected)}"]`).focus(); updateRunBtn(); });
      b.addEventListener("keydown", (e) => {
        const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
        if (d) { e.preventDefault(); const n = btns[(i + d + btns.length) % btns.length]; n.click(); }
      });
    });
  }

  function updateRunBtn() {
    const running = es !== null;
    $("#run-btn").disabled = running || (!customMode() && !selected);
    $("#run-btn").textContent = running ? "Running…" : "Downshift it";
  }

  /* ---------- start a run ---------- */
  function customSpec() {
    const f = new FormData($("#custom-form"));
    const lines = String(f.get("examples") || "").split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    const examples = lines.map((l, i) => {
      try { const o = JSON.parse(l); return typeof o === "string" ? { input: o } : o; }
      catch (e) { throw new Error(`Example line ${i + 1} is not valid JSON.`); }
    });
    const labels = String(f.get("label_set") || "").split(",").map((s) => s.trim()).filter(Boolean);
    return { title: f.get("title") || undefined, instruction: f.get("instruction"), output_format: f.get("output_format") || undefined,
             label_set: labels.length ? labels : undefined, examples };
  }

  $("#run-btn").addEventListener("click", async () => {
    const err = $("#start-error"); err.hidden = true;
    const options = { tolerance: Number($("#opt-tol").value) / 100, spend_cap_usd: Number($("#opt-cap").value), rounds: Number($("#opt-rounds").value),
                      ultra_thinking: $("#opt-ultra-think").checked, thinking_rungs: $("#opt-think-rungs").checked };
    let body;
    try { body = customMode() ? { custom: customSpec(), options } : { preset: selected, options }; }
    catch (e) { err.textContent = e.message; err.hidden = false; return; }
    $("#run-btn").disabled = true;
    try {
      const r = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || "Could not start the run.");
      history.pushState(null, "", j.page_url);
      openRun(j.run_id);
    } catch (e) { err.textContent = e.message; err.hidden = false; updateRunBtn(); }
  });

  /* ---------- streaming ---------- */
  function openRun(runId) {
    if (es) es.close();
    S = freshState(); S.runId = runId;
    ["#ladder-panel", "#log-panel"].forEach((s) => ($(s).hidden = false));
    $("#rounds-panel").hidden = true; $("#verdict-panel").hidden = true;
    $("#log").innerHTML = ""; $("#rounds").innerHTML = "";
    $("#bar-line").textContent = "Starting…";
    renderLadder();
    es = new EventSource(`/api/run/${encodeURIComponent(runId)}/events`);
    updateRunBtn();
    const types = ["started", "stage", "references", "checker_attempt", "grader", "notice", "skipped", "eval", "bar", "round", "tier_done", "verdict", "aborted", "error", "done"];
    types.forEach((t) => es.addEventListener(t, (m) => handle(JSON.parse(m.data))));
    es.addEventListener("end", () => { es.close(); es = null; updateRunBtn(); });
    es.onerror = () => { if (es && es.readyState === EventSource.CLOSED) { es = null; updateRunBtn(); } };
    $("#ladder-panel").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }

  function log(text) {
    const li = document.createElement("li"); li.textContent = text; $("#log").appendChild(li);
  }

  function row(k) { return (S.rows[k] ||= { plain: null, holdout: null, status: "waiting" }); }

  function handle(e) {
    if (e.spend_usd != null) { S.spend = e.spend_usd; renderSpend(); }
    switch (e.type) {
      case "started":
        tiers = e.tiers; S.cap = e.options.spend_cap_usd; S.rowPts = e.row_points;
        $("#task-story").textContent = (e.task.title ? e.task.title + ". " : "") + (e.task.business_story || "");
        $("#split-note").textContent = `${e.n_train} rows are used for tuning prompts and ${e.n_holdout} are held out. Held-out rows are never shown to the prompt optimiser. Prices: ${e.pricing_source}.`;
        log(`Run ${e.run_id} started`);
        break;
      case "stage":
        log(e.message);
        if (e.stage === "bar") row(e.tier_key || "ultra").status = "running";
        if (e.stage === "downshift") { S.currentTier = e.tier_key; row(e.tier_key).status = "tuning"; }
        $("#bar-line").textContent = e.message + "…";
        break;
      case "references": log(`Ultra wrote ${e.n} reference outputs`); break;
      case "checker_attempt":
        log(`Checker attempt ${e.attempt}: references scored ${pct(e.validation.ref_mean)}, wrong answers scored ${pct(e.validation.wrong_mean)} -> ${e.validation.ok ? "accepted" : "rejected"}`);
        break;
      case "grader": {
        const v = e.validation;
        log(`Grader: ${e.source === "preset" ? "the task's checker" : e.source === "ultra" ? "checker written by Ultra" : "exact match"}; references score ${pct(v.ref_mean)}, wrong answers ${pct(v.wrong_mean)}`);
        if (v.backend) noteGrader(v.backend);
        break;
      }
      case "notice": log(e.message); break;
      case "skipped": row(e.tier_key).status = "capped"; log(e.message); break;
      case "eval": {
        noteGrader(e.grader);
        const r = row(e.tier_key);
        if (e.split === "train" && e.label === "plain") { r.plain = e; if (!e.tier_key.startsWith("ultra")) r.status = "tuning"; }
        if (e.split === "holdout") { r.holdout = e; }
        if (e.tier_key.startsWith("ultra") && e.split === "holdout") r.status = "measured";
        log(`${tierName(e.tier_key)} · ${e.split === "holdout" ? "held out" : "tuning"} · ${e.label}: ${pct(e.score)} on ${e.n}, ${usd(e.cost_per_1k_usd)} per 1k calls, p50 ${ms(e.p50_ms)}${e.truncated ? ` · ${e.truncated} ran out of tokens` : ""}${e.grade_batch && e.grade_batch.operation_id ? " · sandbox op " + e.grade_batch.operation_id.slice(0, 8) : ""}`);
        break;
      }
      case "bar":
        S.bar = e.bar; S.trainBar = e.train_bar; S.ref = e.reference; S.rowPts = e.row_points;
        row(e.reference).status = "reference";
        S.barText = `Bar: ${pct(e.bar)} on held-out rows (${tierName(e.reference)} scored ${pct(e.ultra_holdout)}, minus ${Math.round(e.tolerance * 100)} points). Resolution: ±1 row = ${e.row_points} points.`;
        break;
      case "round":
        (S.rounds[e.tier_key] ||= []).push(e);
        $("#rounds-panel").hidden = false;
        renderRounds();
        log(`${tierName(e.tier_key)} round ${e.round}: best on tuning rows ${pct(e.best_score)}`);
        break;
      case "tier_done":
        row(e.tier_key).status = e.passed ? "passed" : "failed";
        log(`${tierName(e.tier_key)}: ${pct(e.holdout_score)} held out vs bar ${pct(e.bar)} -> ${e.passed ? "meets the bar" : "below the bar"}`);
        break;
      case "verdict":
        S.verdict = e.verdict;
        if (S.barText) $("#bar-line").textContent = S.barText;
        tiers.forEach((t) => { const r = row(t.key); if (["waiting", "running"].includes(r.status)) r.status = "skipped"; });
        renderVerdict();
        break;
      case "aborted":
        log("Stopped: " + e.reason);
        $("#bar-line").textContent = "Run stopped at its spend cap. " + e.reason;
        break;
      case "error":
        log("Error: " + e.message);
        $("#bar-line").textContent = "The run failed: " + e.message;
        break;
      case "done":
        log(`Finished. ${e.calls ?? "?"} model calls, ${usd(e.spend_usd)} spent.`);
        tiers.forEach((t) => { const r = row(t.key); if (r.status === "running" || r.status === "tuning") r.status = "stopped"; });
        break;
    }
    renderLadder();
  }

  function noteGrader(g) {
    if (!g || g === "none") return;
    S.graders.add(g);
    setGraderBadge(S.graders.has("local") ? "local" : "sandbox");
  }

  const tierName = (k) => { const t = tiers.find((x) => x.key === k); return t ? (t.label || t.name) : k; };

  function renderSpend() {
    $("#spend").textContent = usd(S.spend);
    $("#cap").textContent = usd(S.cap, 2);
    $("#spend-fill").style.width = Math.min(100, (100 * S.spend) / (S.cap || 1)) + "%";
  }

  function scoreCell(e, bar, label) {
    if (!e) return '<span class="muted">–</span>';
    const pass = bar != null ? e.score >= bar - 1e-9 : null;
    const mark = bar != null ? `<span class="mark" style="left:${Math.min(100, bar * 100)}%" title="bar"></span>` : "";
    return `<div class="score"><span class="v">${pct(e.score)} <span class="lbl">n=${e.n}${label ? " · " + esc(label) : ""}</span></span>
      <span class="track" aria-hidden="true"><span class="fill ${pass == null ? "" : pass ? "pass" : "fail"}" style="width:${e.score * 100}%"></span>${mark}</span>${e.truncated ? `<span class="trunc">${e.truncated} of ${e.n} ran out of tokens</span>` : ""}</div>`;
  }

  const STATUS = {
    waiting: ["", "Waiting"], running: ["run", "Measuring"], measured: ["", "Measured"], tuning: ["run", "Rewriting prompt"],
    passed: ["pass", "Meets the bar"], failed: ["fail", "Below the bar"], reference: ["ref", "Sets the bar"], skipped: ["", "Not needed"], stopped: ["", "Stopped"],
    capped: ["", "Skipped (spend cap)"],
  };

  function renderLadder() {
    if (!S) return;
    const chosen = S.verdict && S.verdict.kind === "downshift" ? S.verdict.rung : null;
    $("#ladder-body").innerHTML = (tiers.length ? tiers : []).map((t) => {
      const r = row(t.key);
      const best = r.holdout || r.plain;
      const [cls, txt] = STATUS[r.status] || ["", r.status];
      const hoLabel = r.holdout ? (r.holdout.label === "plain" ? "plain" : r.holdout.label === "carried" ? "carried" : "rewritten") : "";
      return `<tr class="${chosen === t.key ? "chosen" : ""}">
        <th scope="row"><div class="tier-name">${esc(t.name)}<span class="think ${t.thinking ? "on" : ""}">thinking ${t.thinking ? "on" : "off"}</span></div><div class="tier-model">${esc(t.model)}</div></th>
        <td class="num">$${t.price_in_per_m} / $${t.price_out_per_m}</td>
        <td>${scoreCell(r.plain, S.trainBar)}</td>
        <td>${scoreCell(r.holdout, S.bar, hoLabel)}</td>
        <td class="num">${best ? usd(best.cost_per_1k_usd) : "–"}</td>
        <td class="num">${best ? ms(best.p50_ms) : "–"}</td>
        <td><span class="chip ${cls}">${txt}</span></td></tr>`;
    }).join("");
  }

  function renderRounds() {
    $("#rounds").innerHTML = Object.entries(S.rounds).map(([k, rounds]) => `
      <div class="tier-rounds"><h3>${esc(tierName(k))}</h3>
      ${rounds.map((r) => `<div class="round"><div class="round-head"><span>Round ${r.round}</span><span>best so far ${pct(r.best_score)}${r.train_bar != null ? " · target " + pct(r.train_bar) : ""}</span></div>
        ${r.candidates.length ? `<ul class="cands">${r.candidates.map((c) => `<li class="cand"><span class="s ${c.score >= r.best_score - 1e-9 && c.label === r.best_label ? "best" : ""}">${pct(c.score)}</span>
          <details><summary>${esc(c.note || c.label)} · ${c.fewshot_ids.length} example${c.fewshot_ids.length === 1 ? "" : "s"} · scored on ${c.n} rows</summary><pre class="mono">${esc(c.system_preview)}${c.system_preview.length >= 240 ? "…" : ""}</pre></details></li>`).join("")}</ul>`
          : `<p class="muted">${esc(r.message || "No candidates")}</p>`}</div>`).join("")}</div>`).join("");
  }

  function renderVerdict() {
    const v = S.verdict, p = $("#verdict-panel");
    p.hidden = false;
    const short = v.tier_name.replace(/^Nemotron\s*/, "") + (v.thinking ? " (thinking on)" : " (thinking off)");
    $("#v-headline").textContent = v.kind === "downshift"
      ? (v.savings_multiple && v.savings_multiple > 1 ? `Use ${short}: ${v.savings_multiple}x cheaper than Ultra` : `Use ${short}`)
      : "Keep Ultra for now";
    $("#v-sentence").textContent = v.sentence;
    const stat = (dt, dd, vs) => `<div><dt>${dt}</dt><dd>${dd}${vs ? `<span class="vs">${vs}</span>` : ""}</dd></div>`;
    $("#v-stats").innerHTML =
      stat("Held-out score", pct(v.holdout_score), `Ultra ${pct(v.ultra_holdout_score)} · bar ${pct(v.bar)} · n=${v.holdout_n}, ±1 row = ${v.row_points} pts`) +
      stat("Cost per 1k calls", usd(v.cost_per_1k_usd), `Ultra ${usd(v.ultra_cost_per_1k_usd)}, thinking ${v.reference.thinking ? "on" : "off"}`) +
      stat("p50 latency", ms(v.p50_latency_ms), `Ultra ${ms(v.ultra_p50_latency_ms)}`) +
      stat("Thinking", v.thinking ? "On" : "Off", "set by enable_thinking in the snippet") +
      stat("This run cost", usd(v.run_spend_usd), `graded by ${v.graded_by.map((g) => (g === "sandbox" ? "Nebius Sandboxes" : "local subprocess")).join(" + ")}`);
    $("#v-prompt").textContent = v.system_prompt + (v.fewshots.length ? `\n\n+ ${v.fewshots.length} example pair(s): ${v.fewshots.map((f) => f.id).join(", ")}` : "");
    $("#v-snippet").textContent = v.snippet;
    $("#share-link").href = `/?run=${S.runId}`;
    $("#v-fine").textContent = `Model: ${v.model}, thinking ${v.thinking ? "on" : "off"}. Compared with ${v.reference.label}. Scores are on held-out rows the optimiser never saw; cost is the mean and latency the median of the calls made in this run.`;
    const ft = $("#v-finetune");
    if (v.finetune_offer) {
      ft.hidden = false;
      const enabled = health && health.finetune_enabled;
      ft.innerHTML = `<p><strong>Prompting tops out here.</strong> The next step is to fine-tune a small open model (Qwen3-4B) on Ultra's answers to the tuning rows, using a Token Factory fine-tune job. Nemotron itself is not fine-tunable on Token Factory, and serving the result needs custom-weights access.</p>
        ${enabled ? `<button type="button" class="ghost" id="ft-btn">Start a fine-tune job (paid)</button> <span id="ft-status" class="muted" aria-live="polite"></span>` : '<p class="muted">Fine-tune launches are turned off on this demo server.</p>'}`;
      const b = $("#ft-btn");
      if (b) b.addEventListener("click", startFinetune);
    } else ft.hidden = true;
    p.focus({ preventScroll: true });
  }

  async function startFinetune() {
    const st = $("#ft-status"); $("#ft-btn").disabled = true; st.textContent = "Starting…";
    try {
      const r = await fetch(`/api/run/${S.runId}/finetune`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }) });
      const j = await r.json(); if (!r.ok) throw new Error(j.detail);
      st.textContent = `Job ${j.job_id}: ${j.status} (${j.n_rows} rows on ${j.base_model})`;
      const poll = async () => {
        const s = await (await fetch(`/api/finetune/${encodeURIComponent(j.job_id)}`)).json();
        st.textContent = `Job ${j.job_id}: ${s.status}`;
        if (!["succeeded", "failed", "cancelled"].includes(s.status)) setTimeout(poll, 30000);
      };
      setTimeout(poll, 30000);
    } catch (e) { st.textContent = e.message; $("#ft-btn").disabled = false; }
  }

  $("#copy-btn").addEventListener("click", async () => {
    const b = $("#copy-btn");
    try { await navigator.clipboard.writeText($("#v-snippet").textContent); b.textContent = "Copied"; }
    catch (e) { const range = document.createRange(); range.selectNodeContents($("#v-snippet")); getSelection().removeAllRanges(); getSelection().addRange(range); b.textContent = "Press Ctrl+C"; }
    setTimeout(() => (b.textContent = "Copy"), 1800);
  });

  /* ---------- boot ---------- */
  async function boot() {
    try {
      health = await (await fetch("/api/health")).json();
      const g = health.grader || {};
      setGraderBadge(g.backend === "sandbox" && !(g.sandbox_info && g.sandbox_info.error) ? "sandbox" : "local");
    } catch (e) { /* badge stays neutral */ }
    try { tiers = await (await fetch("/api/tiers")).json(); } catch (e) {}
    try {
      presets = await (await fetch("/api/presets")).json();
      if (!selected && presets.length) selected = presets[0].slug;
    } catch (e) { presets = []; }
    renderPresets(); updateRunBtn();
    const runId = new URLSearchParams(location.search).get("run");
    if (runId) openRun(runId);
  }
  window.addEventListener("popstate", () => { const id = new URLSearchParams(location.search).get("run"); if (id) openRun(id); });
  boot();
})();
