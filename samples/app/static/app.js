const BOOMS = [
  ["zero-division", "Divide by zero",    "ZeroDivisionError in orders.py"],
  ["key-error",     "Missing key",       "KeyError on an unvalidated payload"],
  ["attribute-error", "Null reference",  "AttributeError on a None customer"],
  ["timeout",       "Upstream timeout",  "looks transient — ticket or not?"],
  ["repeat-bug",    "Same bug, moved",   "same defect, new line — duplicate test"],
];

const out = document.getElementById("out");
const esc = s => String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

BOOMS.forEach(([slug, title, desc]) => {
  const b = document.createElement("button");
  b.type = "button";
  b.innerHTML = `<span class="t">${title}</span><span class="d">${desc}</span>`;
  b.addEventListener("click", () => trigger(slug, title));
  document.getElementById("buttons").appendChild(b);
});

fetch("/api/mode").then(r => r.json()).then(m => {
  const el = document.getElementById("badge");
  el.textContent = m.dry_run ? "DRY RUN" : "LIVE — writes to " + m.repo;
  el.className = "badge " + (m.dry_run ? "dry" : "live");
});

async function trigger(slug, title) {
  out.innerHTML = `<p class="empty">Triggering ${esc(title)}…</p>`;
  const r = await fetch("/boom/" + slug, { method: "POST" });
  const body = await r.json();
  out.innerHTML = `
    <div class="card">
      <h3>${esc(title)} — captured</h3>
      <div class="meta">HTTP ${r.status} · incident ${esc(body.incident_id ?? "?")}</div>
      <pre>${esc(JSON.stringify(body, null, 2))}</pre>
      <div class="meta">Captured in-process from the live exception — nothing
        written to disk. Now write a ticket.</div>
    </div>`;
}

async function callEndpoint(path, label) {
  out.innerHTML = `<p class="empty">${label}…</p>`;
  let r, data;
  try {
    r = await fetch(path, { method: "POST" });
    data = await r.json();
  } catch (e) {
    out.innerHTML = `<div class="card"><h3 class="err">Request failed</h3><pre>${esc(e)}</pre></div>`;
    return;
  }
  if (!r.ok) {
    out.innerHTML = `<div class="card">
      <h3 class="err">${esc(data.error ?? "Something went wrong")}</h3>
      <pre>${esc(data.detail ?? JSON.stringify(data, null, 2))}</pre></div>`;
    return;
  }
  render(data);
}

document.getElementById("run").addEventListener("click",
  () => callEndpoint("/write-ticket", "Assembling context and calling the model"));
document.getElementById("inspect").addEventListener("click",
  () => callEndpoint("/context", "Assembling context"));

function render(d) {
  let html = "";

  if (d.redactions) {
    const hits = Object.entries(d.redactions);
    const body = hits.length
      ? hits.map(([rule, n]) =>
          `<span class="tag">${esc(rule)} × ${n}</span>`).join("")
      : `<span class="meta">nothing matched — no secrets found in this incident</span>`;
    html += `<div class="step"><h2>Redacted before sending</h2>
      <div class="card">${body}</div></div>`;
  }

  if (d.context_text) {
    html += `<div class="step"><h2>What the model is sent</h2>
      <pre>${esc(d.context_text)}</pre></div>`;
  }

  if (d.searched) {
    html += `<div class="step"><h2>What the agent did</h2>
      <pre>${esc(d.searched)}</pre></div>`;
  }

  const g = d.guardrails;
  if (g && Object.keys(g).length) {
    const refusals = (g.refusals || []).length
      ? `<pre>${esc(g.refusals.join("\n"))}</pre>`
      : `<span class="meta">no tool calls refused</span>`;
    html += `<div class="step"><h2>Guardrails</h2>
      <div class="card">
        <span class="tag">writes ${g.writes_used} / ${g.write_cap}</span>
        ${d.dry_run ? '<span class="tag">dry run — write tools not loaded</span>' : ""}
        <div style="margin-top:10px">${refusals}</div>
      </div></div>`;
  }

  const t = d.ticket;
  if (t) {
    const dup = t.duplicate_of
      ? `<div class="meta">matched existing issue #${t.duplicate_of}</div>` : "";
    html += `<div class="step"><h2>Generated ticket · ${esc(t.action)}</h2>
      <div class="card">
        <h3>${esc(t.title)}</h3>
        <div class="meta">
          <span class="tag">${esc(t.severity)}</span>
          ${(t.labels || []).map(l => `<span class="tag">${esc(l)}</span>`).join("")}
          confidence ${t.confidence}
        </div>
        ${dup}
        <pre>${esc(t.description_markdown)}</pre>
        <h3>Root cause</h3><p>${esc(t.root_cause)}</p>
        <h3>Suggested fix</h3><pre>${esc(t.suggested_fix)}</pre>
        <div class="meta"><strong>Why:</strong> ${esc(t.reasoning)}</div>
      </div></div>`;
  }

  out.innerHTML = html || `<p class="empty">Nothing came back.</p>`;
}
