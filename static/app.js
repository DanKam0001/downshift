"use strict";
(() => {
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pct = (x) => (x == null ? "–" : (Math.round(x * 1000) / 10).toString().replace(/\.0$/, "") + "%");
  const usd = (x, d = 4) => (x == null ? "–" : "US$" + Number(x).toFixed(d));
  const pts = (x) => (x == null ? "–" : (x >= 0 ? "+" : "−") + Math.abs(x * 100).toFixed(1));
  const ciTxt = (ci) => (ci ? `${pct(ci.lo)}–${pct(ci.hi)}` : "");
  const ms = (x) => (x == null ? "–" : x < 1000 ? Math.round(x) + " ms" : (x / 1000).toFixed(2) + " s");

  let presets = [], selected = null, tiers = [], es = null, health = null;
  let S = null; // per-run view state
  let replayTimer = null;

  function freshState() {
    return { rows: {}, rounds: {}, verdict: null, cap: 0.1, spend: 0, graders: new Set(), currentTier: null, runId: null, ref: null, refInfo: null, tol: 0.03, rowPts: null };
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
                      ultra_thinking: $("#opt-ultra-think").checked, thinking_rungs: $("#opt-think-rungs").checked,
                      reference: $("#opt-live-ref").checked ? "live" : "cached" };
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
    const speed = Number(new URLSearchParams(location.search).get("speed")) || 0;
    clearInterval(replayTimer);
    $("#replay-note").hidden = !speed;
    if (speed) {
      const t0 = performance.now();
      const tick = () => {
        const s = Math.round(((performance.now() - t0) / 1000) * speed);
        $("#replay-note").textContent = `Replay of recorded run ${runId} at ${speed}x speed · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} of real run time`;
      };
      tick(); replayTimer = setInterval(tick, 250);
    }
    es = new EventSource(`/api/run/${encodeURIComponent(runId)}/events${speed ? `?speed=${speed}` : ""}`);
    updateRunBtn();
    const types = ["started", "stage", "references", "checker_attempt", "grader", "notice", "skipped", "eval", "bar", "round", "tier_done", "verdict", "photo", "guess", "squeeze", "aborted", "error", "done"];
    types.forEach((t) => es.addEventListener(t, (m) => handle(JSON.parse(m.data))));
    es.addEventListener("end", () => { es.close(); es = null; updateRunBtn(); });
    es.onerror = () => { if (es && es.readyState === EventSource.CLOSED) { es = null; updateRunBtn(); } };
    $("#ladder-panel").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }

  function log(text) {
    const li = document.createElement("li"); li.textContent = text; $("#log").appendChild(li);
  }

  function row(k) { return (S.rows[k] ||= { plain: null, val: null, holdout: null, done: null, ref: null, status: "waiting" }); }

  function handle(e) {
    if (e.spend_usd != null) { S.spend = e.spend_usd; renderSpend(); }
    switch (e.type) {
      case "started":
        tiers = e.tiers; S.cap = e.options.spend_cap_usd; S.rowPts = e.row_points;
        if (e.task.slug && presets.some((p) => p.slug === e.task.slug)) { selected = e.task.slug; renderPresets(); }
        $("#task-story").textContent = (e.task.title ? e.task.title + ". " : "") + (e.task.business_story || "");
        $("#split-note").textContent = `${e.n_train} tuning rows (the prompt optimiser sees these), ${e.n_validation} validation rows (used only to choose between prompt candidates) and ${e.n_holdout} held-out rows (each rung's chosen prompt is scored on them once). Confidence intervals are 95% bootstrap intervals over rows; the comparison with Ultra is paired on the same rows. Prices: ${e.pricing_source}.`;
        log(`Run ${e.run_id} started`);
        break;
      case "stage":
        log(e.message);
        if (e.stage === "bar") row(e.tier_key || "ultra").status = "running";
        if (e.stage === "downshift") { S.currentTier = e.tier_key; row(e.tier_key).status = "tuning"; }
        if (e.stage === "photo") row(e.tier_key).status = "running";
        if (e.stage === "squeeze") row(e.tier_key).status = "running";
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
        log(`${tierName(e.tier_key)} · ${{ holdout: "held out", validation: "validation", train: "tuning" }[e.split] || e.split} · ${e.label}: ${pct(e.score)} on ${e.n}, ${usd(e.cost_per_1k_usd)} per 1k calls, p50 ${ms(e.p50_ms)}${e.truncated ? ` · ${e.truncated} ran out of tokens` : ""}${e.grade_batch && e.grade_batch.branched ? ` · graded in a branch of sealed answer key ${e.grade_batch.checkpoint.slice(0, 8)}` : e.grade_batch && e.grade_batch.operation_id ? " · sandbox op " + e.grade_batch.operation_id.slice(0, 8) : ""}`);
        break;
      }
      case "bar": {
        S.ref = e.reference; S.refInfo = e; S.tol = e.tolerance; S.rowPts = e.row_points;
        Object.entries(e.modes || {}).forEach(([k, m]) => { const r = row(k); r.ref = m; r.status = k === e.reference ? "reference" : "measured"; });
        const runs = e.repeat_scores || [];
        const how = e.reference_source === "cached"
          ? `${tierName(e.reference)} reference measured on ${e.measured_on}, ${e.repeats} runs, mean ${pct(e.ultra_holdout)} ± ${(Math.abs(e.spread) * 50).toFixed(1)} points (runs: ${runs.map(pct).join(", ")})`
          : `${tierName(e.reference)} measured live in this run (one pass): ${pct(e.ultra_holdout)}`;
        S.barText = `${how}; 95% CI over rows ${ciTxt(e.ultra_ci)}. A cheaper rung matches Ultra only if the 95% CI of its paired difference from Ultra stays above −${Math.round(e.tolerance * 100)} points.`;
        $("#bar-line").textContent = S.barText;
        log(S.barText);
        break;
      }
      case "round":
        (S.rounds[e.tier_key] ||= []).push(e);
        $("#rounds-panel").hidden = false;
        renderRounds();
        log(`${tierName(e.tier_key)} round ${e.round}: best so far ${pct(e.best_val)} on validation (${pct(e.best_score)} on tuning)`);
        break;
      case "tier_done":
        row(e.tier_key).done = e;
        row(e.tier_key).status = e.passed ? (e.beats ? "beats" : "passed") : "failed";
        log(e.message || `${tierName(e.tier_key)}: ${e.passed ? "matches" : "not shown to match"}`);
        break;
      case "verdict":
        S.verdict = e.verdict;
        if (S.barText) $("#bar-line").textContent = S.barText;
        tiers.forEach((t) => { const r = row(t.key); if (!t.photo && ["waiting", "running"].includes(r.status)) r.status = "skipped"; });
        renderVerdict();
        break;
      case "guess":
        S.guess = e; log(e.message);
        if (e.tripped) $("#bar-line").textContent = e.message;
        break;
      case "squeeze":
        if (S.verdict) row(e.tier_key).status = S.verdict.kind === "downshift" ? "passed" : "measured";
        S.squeeze = e; renderSqueeze();
        if (S.barText) $("#bar-line").textContent = S.barText;
        log(e.message);
        break;
      case "photo":
        row(e.tier_key).done = e;
        row(e.tier_key).status = e.passed ? (e.beats ? "beats" : "passed") : "failed";
        S.photo = e; renderPhoto();
        if (S.barText) $("#bar-line").textContent = S.barText;
        log(e.message);
        break;
      case "aborted":
        log("Stopped: " + e.reason);
        $("#bar-line").textContent = "Run stopped at its spend cap. " + e.reason;
        break;
      case "error":
        log("Error: " + e.message);
        $("#bar-line").textContent = "The run failed: " + e.message + " The recorded runs under the task picker replay without any model calls.";
        break;
      case "done":
        clearInterval(replayTimer);
        log(`Finished. ${e.calls ?? "?"} model calls, ${usd(e.spend_usd)} spent${e.seconds != null ? `, ${Math.round(e.seconds)} s` : ""}.`);
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

  function bar(score, ci, cls) {
    const band = ci ? `<span class="band" style="left:${ci.lo * 100}%;width:${Math.max(0.5, (ci.hi - ci.lo) * 100)}%"></span>` : "";
    return `<span class="track" aria-hidden="true">${band}<span class="fill ${cls || ""}" style="width:${score * 100}%"></span></span>`;
  }

  function tvCell(r) {
    const d = r.done;
    if (d && d.tuning_score != null) {
      return `<div class="score"><span class="v">${pct(d.tuning_score)} / ${pct(d.validation_score)} <span class="lbl">${esc(d.prompt_label === "plain" ? "plain" : d.prompt_label === "carried" ? "carried" : "rewritten")}</span></span></div>`;
    }
    if (r.plain) return `<div class="score"><span class="v">${pct(r.plain.score)} <span class="lbl">plain, tuning n=${r.plain.n}</span></span></div>`;
    return '<span class="muted">–</span>';
  }

  function holdoutCell(r) {
    if (r.ref) {
      const m = r.ref, runs = m.repeat_scores || [];
      const sub = runs.length > 1 ? `${runs.length} runs: ${runs.map(pct).join(" / ")}` : "one pass";
      return `<div class="score"><span class="v">${pct(m.mean)} <span class="lbl">CI ${ciTxt(m.ci)} · ${esc(sub)}</span></span>${bar(m.mean, m.ci, "ref")}</div>`;
    }
    const d = r.done;
    if (d) {
      const cls = d.passed ? "pass" : "fail";
      const vs = d.diff ? `vs Ultra ${pts(d.diff.diff)} pts (CI ${pts(d.diff.lo)} to ${pts(d.diff.hi)})` : "";
      return `<div class="score"><span class="v">${pct(d.holdout_score)} <span class="lbl">CI ${ciTxt(d.ci)}</span></span>${bar(d.holdout_score, d.ci, cls)}<span class="lbl">${vs}</span>${r.holdout && r.holdout.truncated ? `<span class="trunc">${r.holdout.truncated} of ${r.holdout.n} ran out of tokens</span>` : ""}</div>`;
    }
    return '<span class="muted">–</span>';
  }

  const STATUS = {
    waiting: ["", "Waiting"], running: ["run", "Measuring"], measured: ["", "Measured"], tuning: ["run", "Rewriting prompt"],
    passed: ["pass", "Matches Ultra"], beats: ["pass", "Better than Ultra"], failed: ["fail", "Not shown to match"], reference: ["ref", "Reference"], skipped: ["", "Not needed"], stopped: ["", "Stopped"],
    capped: ["", "Skipped (spend cap)"],
  };

  const modeChip = (t) => t.photo ? '<span class="think photo">reads the photo</span>'
    : `<span class="think ${t.thinking ? "on" : ""}">thinking ${t.thinking ? "on" : "off"}</span>`;

  function renderLadder() {
    if (!S) return;
    const chosen = S.verdict && S.verdict.kind === "downshift" ? S.verdict.rung : null;
    $("#ladder-body").innerHTML = (tiers.length ? tiers : []).map((t) => {
      const r = row(t.key);
      const best = r.ref || r.holdout || r.plain;
      const [cls, txt] = STATUS[r.status] || ["", r.status];
      return `<tr class="${chosen === t.key ? "chosen" : ""}${t.photo ? " photo-row" : ""}">
        <th scope="row"><div class="tier-name">${esc(t.name)}${modeChip(t)}</div><div class="tier-model">${esc(t.model)}</div></th>
        <td class="num">$${t.price_in_per_m} / $${t.price_out_per_m}</td>
        <td>${tvCell(r)}</td>
        <td>${holdoutCell(r)}</td>
        <td class="num">${best ? usd(best.cost_per_1k_usd) : "–"}</td>
        <td class="num">${best ? ms(best.p50_ms) : "–"}</td>
        <td><span class="chip ${cls}">${txt}</span></td></tr>`;
    }).join("");
    renderCosts(chosen);
  }

  // One bar per rung, US$ per 1k calls on a shared scale, coloured by what the run found.
  function renderCosts(chosen) {
    const cost = (r) => { const b = r.ref || (r.done && r.holdout) || r.holdout || r.plain; return b ? b.cost_per_1k_usd : null; };
    const max = Math.max(0, ...tiers.map((t) => cost(row(t.key)) || 0));
    const refCost = S.ref ? cost(row(S.ref)) : null;
    $("#costs").innerHTML = tiers.map((t) => {
      const r = row(t.key), c = cost(r);
      const kind = chosen === t.key ? "pass" : r.status === "reference" ? "ref" : r.status === "failed" ? "fail"
        : ["passed", "beats"].includes(r.status) ? "pass" : ["tuning", "running"].includes(r.status) ? "run" : "";
      const x = chosen === t.key && refCost && c ? ` · ${(refCost / c).toFixed(1)}x cheaper` : kind === "ref" ? " · reference" : "";
      return `<div class="cost ${kind} ${c == null ? "empty" : ""}">
        <span class="cname">${esc(t.name.replace(/^Nemotron\s*/, ""))}${modeChip(t)}</span>
        <span class="ctrack"><span class="cfill" style="width:${c && max ? Math.max(1.2, (100 * c) / max) : 0}%"></span></span>
        <span class="cval">${c == null ? "–" : usd(c, 3) + " / 1k" + x}</span></div>`;
    }).join("");
  }

  function renderRounds() {
    $("#rounds").innerHTML = Object.entries(S.rounds).map(([k, rounds]) => `
      <div class="tier-rounds"><h3>${esc(tierName(k))}</h3>
      ${rounds.map((r) => `<div class="round"><div class="round-head"><span>Round ${r.round}</span><span>best so far ${pct(r.best_val)} validation${r.validation_target != null ? " · stop at " + pct(r.validation_target) : ""}</span></div>
        ${r.candidates.length ? `<ul class="cands">${r.candidates.map((c) => `<li class="cand"><span class="s ${c.label === r.best_label ? "best" : ""}" title="validation score">${pct(c.val)}</span>
          <details><summary>${esc(c.note || c.label)} · ${c.fewshot_ids.length} example${c.fewshot_ids.length === 1 ? "" : "s"} · tuning ${pct(c.score)} on ${c.n} rows, validation ${pct(c.val)} on ${c.n_val}</summary><pre class="mono">${esc(c.system_preview)}${c.system_preview.length >= 240 ? "…" : ""}</pre></details></li>`).join("")}</ul>`
          : `<p class="muted">${esc(r.message || "No candidates")}</p>`}</div>`).join("")}</div>`).join("");
  }

  function renderVerdict() {
    const v = S.verdict, p = $("#verdict-panel");
    p.hidden = false;
    const short = v.tier_name.replace(/^Nemotron\s*/, "") + (v.thinking ? " (thinking on)" : " (thinking off)");
    const tolPts = Math.round(v.tolerance * 100);
    $("#v-headline").textContent = v.kind === "no_verdict" ? "No verdict: this checker can't tell skill from guessing"
      : v.kind !== "downshift" ? "Keep Ultra for now"
      : v.thinking_off_only ? `Turn thinking off: same quality within ${tolPts} points, ${v.savings_multiple}x cheaper`
      : (v.savings_multiple && v.savings_multiple > 1 ? `Use ${short}: ${v.savings_multiple}x cheaper, matches Ultra within ${tolPts} points (95% CI)` : `Use ${short}`);
    $("#v-sentence").textContent = v.sentence + (v.kind === "no_verdict" ? "" : ` (Rule: matches = the 95% CI of the difference from Ultra stays within ${tolPts} points of Ultra; better = the whole CI is above Ultra.)`);
    $("#v-prompt-box").hidden = $("#v-snippet-box").hidden = v.kind === "no_verdict";
    const stat = (dt, dd, vs) => `<div><dt>${dt}</dt><dd>${dd}${vs ? `<span class="vs">${vs}</span>` : ""}</dd></div>`;
    $("#v-stats").innerHTML =
      stat("Held-out score", pct(v.holdout_score), `95% CI ${ciTxt(v.holdout_ci)} · n=${v.holdout_n}`) +
      stat("Ultra reference", pct(v.reference.mean), v.reference.description) +
      (v.diff_vs_reference ? stat("Difference vs Ultra", pts(v.diff_vs_reference.diff) + " pts", `95% CI ${pts(v.diff_vs_reference.lo)} to ${pts(v.diff_vs_reference.hi)}, paired on the same rows`) : "") +
      (v.overfit && v.overfit.tuning_score != null ? stat("Tuning → validation → held out", `${pct(v.overfit.tuning_score)} → ${pct(v.overfit.validation_score)} → ${pct(v.overfit.holdout_score)}`, "the drop is how much the prompt overfit the rows it was tuned on") : "") +
      stat("Cost per 1k calls", usd(v.cost_per_1k_usd), `Ultra ${usd(v.ultra_cost_per_1k_usd)}, thinking ${v.reference.thinking ? "on" : "off"}`) +
      stat("p50 latency", ms(v.p50_latency_ms), `Ultra ${ms(v.ultra_p50_latency_ms)}`) +
      stat("Thinking", v.thinking ? "On" : "Off", "set by enable_thinking in the snippet") +
      stat("This run cost", usd(v.run_spend_usd), `${v.run_seconds != null ? Math.round(v.run_seconds) + " s · " : ""}graded by ${v.graded_by.map((g) => (g === "sandbox" ? "Nebius Sandboxes" : "local subprocess")).join(" + ")}`);
    renderSavings(); renderSqueeze(); renderPhoto(); renderGuess();
    $("#v-prompt").textContent = v.system_prompt + (v.fewshots.length ? `\n\n+ ${v.fewshots.length} example pair(s): ${v.fewshots.map((f) => f.id).join(", ")}` : "");
    $("#v-snippet").textContent = v.snippet;
    $("#share-link").href = `/?run=${S.runId}`;
    $("#v-fine").textContent = `Model: ${v.model}, thinking ${v.thinking ? "on" : "off"}. Compared with ${v.reference.label} (${v.reference.source === "cached" ? "cached reference" : "measured live"}). Held-out rows were never seen by the optimiser and never used to choose a prompt; cost is the mean and latency the median of the measured calls.`;
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

  /* ---------- guess check: does the checker reward skill, or would a fixed answer do as well? ---------- */
  function renderGuess() {
    const g = S && S.guess, box = $("#v-guess");
    box.hidden = !g || !g.best;
    if (box.hidden) return;
    box.className = "guess-check " + (g.tripped ? "fail" : "pass");
    box.innerHTML = `<h3>Guess check: ${g.tripped ? "failed, so no model verdict" : "passed"}</h3>
      <p>With no model at all, ${esc(g.best.label)} scores <strong>${pct(g.best.score)}</strong>; Ultra scores <strong>${pct(g.ultra_score)}</strong>
        (Ultra minus the guess: ${pts(g.diff.diff)} pts, 95% CI ${pts(g.diff.lo)} to ${pts(g.diff.hi)}).</p>
      <p class="sav-note">${g.tripped ? "If Ultra can't provably beat a fixed answer, the checker can't tell a good answer from a lucky one, and \"a cheaper model matches\" would mean nothing. Fix the checker first." : "Ultra beats every fixed answer, so the checker measures skill and the comparison below means something."}
        Guess: <code>${esc(g.best.answer.slice(0, 120))}</code></p>`;
  }

  /* ---------- squeeze: the model is chosen, now cut its tokens; every cut checked, the result re-proved ---------- */
  function renderSqueeze() {
    const e = S && S.squeeze, box = $("#v-squeeze");
    box.hidden = !e || !S.verdict;
    if (box.hidden) return;
    const tok = (x) => x.in_tokens != null ? `${Math.round(x.in_tokens)} tokens in, ${Math.round(x.out_tokens)} out` : "";
    const steps = e.steps.map((x) => x.lever === "start"
      ? `<li><span class="sq-l">${esc(tierName(e.rung))}, the verdict's setup</span><span class="sq-c">${usd(x.cost_per_1k_usd, 4)} / 1k</span><span class="sq-t">${pct(x.val)} on validation · ${tok(x)}</span></li>`
      : `<li class="${x.kept ? "" : "drop"}"><span class="sq-l">${x.kept ? "Kept" : "Dropped"}: ${esc(x.label)}</span><span class="sq-c">${x.cost_per_1k_usd != null ? usd(x.cost_per_1k_usd, 4) + " / 1k" : "–"}</span><span class="sq-t">${x.val != null ? pct(x.val) + " on validation · " + tok(x) : esc(x.note || "")}${x.kept ? "" : x.val != null ? " · score fell or cost didn't" : ""}</span></li>`).join("");
    const ref = `<li><span class="sq-l">Ultra (${S.verdict.reference.thinking ? "thinking on" : "thinking off"}), the reference</span><span class="sq-c">${usd(e.reference_cost_per_1k_usd, 3)} / 1k</span></li>`;
    box.className = "squeeze " + (e.applied ? "pass" : "fail");
    box.innerHTML = `<h3>Squeeze: the model is picked, now cut its tokens</h3>
      <p class="sq-call">${e.applied ? `${usd(e.cost_per_1k_usd, 4)} per 1k calls: ${e.x_cheaper}x cheaper than Ultra` : "Nothing to cut: the verdict's setup stays"}</p>
      <ol>${ref}${steps}</ol>
      ${e.applied ? `<p>The squeezed setup, scored once on the ${e.ci.n} held-out rows: <strong>${pct(e.holdout_score)}</strong> (95% CI ${ciTxt(e.ci)}), ${pts(e.diff.diff)} pts vs Ultra (CI ${pts(e.diff.lo)} to ${pts(e.diff.hi)}), so it still matches within ${Math.round(e.tolerance * 100)} points.</p>` : e.holdout_score != null ? `<p>The squeezed setup scored ${pct(e.holdout_score)} on held-out rows and was not shown to match Ultra, so it is not used.</p>` : ""}
      <p class="sav-note">Each cut is tried on validation rows and kept only if the score holds within 1 point and the cost falls. Token Factory also prices cached prompt tokens lower, but we measured no cache hits on Nemotron, so no saving is assumed.</p>`;
    if (e.applied && e.snippet) $("#v-snippet").textContent = e.snippet;
  }

  /* ---------- photo check: a vision model reading the photo instead of the OCR text ---------- */
  function renderPhoto() {
    const e = S && S.photo, box = $("#v-photo");
    box.hidden = !e;
    if (!e) return;
    const vsUltra = e.reference_cost_per_1k_usd && e.cost_per_1k_usd ? (e.reference_cost_per_1k_usd / e.cost_per_1k_usd).toFixed(1) + "x cheaper than Ultra" : "";
    const call = e.beats ? "Better than Ultra, with no OCR step" : e.passed ? "Matches Ultra, with no OCR step" : "Not shown to match Ultra from the photo";
    box.className = "photo-check " + (e.passed ? "pass" : "fail");
    box.innerHTML = `<h3>Photo check: do you need the OCR step at all?</h3>
      <p class="pc-call">${esc(call)}</p>
      <p>${esc(e.label)} read the ${e.ci ? e.ci.n : ""} held-out receipt photos directly, with ${esc(e.prompt)}: <strong>${pct(e.holdout_score)}</strong>
        (95% CI ${ciTxt(e.ci)}), against Ultra's ${pct(S.verdict ? S.verdict.reference.mean : null)} reading the OCR text.
        Difference ${pts(e.diff.diff)} pts (95% CI ${pts(e.diff.lo)} to ${pts(e.diff.hi)}), paired on the same receipts.</p>
      <p>${usd(e.cost_per_1k_usd)} per 1k calls${vsUltra ? " · " + vsUltra : ""}${e.verdict_cost_per_1k_usd ? ` · the text verdict above costs ${usd(e.verdict_cost_per_1k_usd)}, but needs OCR first` : ""} · p50 ${ms(e.p50_ms)}.</p>
      <p class="sav-note">One pass in this run. The text side is the dataset's annotated OCR, i.e. perfect OCR; a real OCR engine would only make the text side worse.</p>`;
  }

  /* ---------- monthly savings, from the verdict's measured cost per 1k calls ---------- */
  const money = (x) => "US$" + Math.round(x).toLocaleString("en-US");
  function callsPerMonth() {
    const n = Number(String($("#calls-month").value).replace(/[^\d.]/g, ""));
    return Number.isFinite(n) && n > 0 ? n : null;
  }
  function renderSavings() {
    const v = S && S.verdict, box = $("#v-savings");
    const ok = v && v.kind === "downshift" && v.cost_per_1k_usd != null && v.ultra_cost_per_1k_usd != null;
    box.hidden = !ok;
    if (!ok) return;
    const n = callsPerMonth(), out = $("#sav-out");
    if (!n) { out.textContent = "Enter a number of calls."; return; }
    const now = (v.ultra_cost_per_1k_usd * n) / 1000, next = (v.cost_per_1k_usd * n) / 1000;
    const who = v.thinking_off_only ? "Ultra with thinking off" : v.tier_name;
    out.innerHTML = `Ultra (thinking ${v.reference.thinking ? "on" : "off"}): <strong>${money(now)}</strong> a month →
      ${esc(who)}: <strong>${money(next)}</strong> a month.
      <span class="sav-big">You save ${money(now - next)} a month, ${money((now - next) * 12)} a year.</span>
      <span class="sav-note">From this run's measured cost per call; your token mix may differ.</span>`;
  }
  try { const saved = localStorage.getItem("ds-calls-month"); if (saved) $("#calls-month").value = saved; } catch (e) {}
  $("#calls-month").addEventListener("input", () => {
    try { localStorage.setItem("ds-calls-month", $("#calls-month").value); } catch (e) {}
    renderSavings();
  });
  $("#calls-month").addEventListener("blur", () => {
    const n = callsPerMonth(); if (n) $("#calls-month").value = n.toLocaleString("en-US");
  });

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
