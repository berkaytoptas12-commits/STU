"use strict";
// Offline single-page UI. No external libraries or network requests besides this server's /api.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  token: safeGet("techrag.token") || "",
  info: null,
  docs: [],
  selected: new Set(), // selected document ids; empty = all sources (automatic routing)
  history: [],         // [{role, content}]
  busy: false,
  notes: loadNotes(),
};

function safeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function safeSet(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } }

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401) {
    const t = prompt("Bu sunucu bir API anahtarı istiyor:");
    if (t) { state.token = t.trim(); safeSet("techrag.token", state.token); return api(path, opts); }
  }
  if (!res.ok) {
    let msg = res.status + " " + res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return res;
}
const getJSON = async (p) => (await api(p)).json();

function fileUrl(docId, page) {
  const q = state.token ? `?token=${encodeURIComponent(state.token)}` : "";
  return `/api/documents/${docId}/file${q}#page=${page || 1}`;
}

// ------------------------------------------------------------- markdown
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function expandCites(inner) {
  const out = [];
  for (const part of inner.split(/\s*,\s*/)) {
    const r = part.match(/^(\d+)\s*[–-]\s*(\d+)$/);
    if (r) { for (let i = +r[1]; i <= +r[2] && i - +r[1] < 50; i++) out.push(i); }
    else if (/^\d+$/.test(part)) out.push(+part);
  }
  return out;
}

function inline(s, cites = true) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s.,;:)]|$)/g, "$1<em>$2</em>");
  if (cites) {
    s = s.replace(/\[(\d+(?:\s*[,–-]\s*\d+)*)\]/g, (m, inner) =>
      expandCites(inner).map((n) => `<button class="cite" data-n="${n}" title="Kaynak ${n}">${n}</button>`).join(""));
  }
  s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => `<code>${codes[+i]}</code>`);
  return s;
}

const LIST_RE = /^\s*([-*•]|\d+[.)])\s+/;

function renderTable(rows, cites = true) {
  const cells = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
  if (!body.length) return "";
  let h = "<table><thead><tr>" + cells(body[0]).map((c) => `<th>${inline(c, cites)}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of body.slice(1)) h += "<tr>" + cells(r).map((c) => `<td>${inline(c, cites)}</td>`).join("") + "</tr>";
  return h + "</tbody></table>";
}

function renderMarkdown(md, cites = true) {
  const inl = (t) => inline(t, cites);
  const lines = esc(md).split("\n");
  let html = "";
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      html += `<pre><code>${buf.join("\n")}</code></pre>`;
      continue;
    }
    if (/^\s*\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      html += renderTable(rows, cites);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const lvl = Math.min(h[1].length + 2, 4);
      html += `<h${lvl}>${inl(h[2])}</h${lvl}>`;
      i++;
      continue;
    }
    if (LIST_RE.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items = [];
      while (i < lines.length && LIST_RE.test(lines[i])) {
        items.push(lines[i].replace(LIST_RE, ""));
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !LIST_RE.test(lines[i])) {
          items[items.length - 1] += " " + lines[i].trim();
          i++;
        }
      }
      const tag = ordered ? "ol" : "ul";
      html += `<${tag}>${items.map((t) => `<li>${inl(t)}</li>`).join("")}</${tag}>`;
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !/^(\s*```|\s*\||#{1,6}\s)/.test(lines[i]) && !LIST_RE.test(lines[i])) {
      para.push(lines[i++]);
    }
    html += `<p>${inl(para.join("<br>"))}</p>`;
  }
  return html;
}

// --------------------------------------------------------------- sources
async function loadInfo() {
  state.info = await getJSON("/api/info");
  const i = state.info;
  $("#model-info").textContent = `LLM: ${i.llm.model} · ${i.stats.documents} doküman · ${i.stats.chunks} parça`;
  $("#upload-box").hidden = !i.allow_upload;
  $("#upload-domain").innerHTML = i.domains.map((d) => `<option value="${esc(d.key)}">${esc(d.name)}</option>`).join("");
  if (i.warmup_error) console.warn("warmup:", i.warmup_error);
}

async function loadDocs() {
  state.docs = await getJSON("/api/documents");
  const ids = new Set(state.docs.map((d) => d.id));
  for (const id of [...state.selected]) if (!ids.has(id)) state.selected.delete(id);
  renderCollections();
}

function renderCollections() {
  const wrap = $("#collections");
  const domains = state.info ? state.info.domains : [];
  wrap.innerHTML = "";
  for (const d of domains) {
    const docs = state.docs.filter((x) => x.domain === d.key);
    const sec = document.createElement("div");
    sec.className = "collection";
    const allSel = docs.length > 0 && docs.every((x) => state.selected.has(x.id));
    const someSel = docs.some((x) => state.selected.has(x.id));
    sec.innerHTML = `<label><input type="checkbox" ${allSel ? "checked" : ""} ${docs.length ? "" : "disabled"}>
      <span title="${esc(d.name)}">${esc(d.key.toUpperCase())}</span><span class="count">${docs.length}</span></label>`;
    const cb = $("input", sec);
    cb.indeterminate = someSel && !allSel;
    cb.addEventListener("change", () => {
      for (const x of docs) cb.checked ? state.selected.add(x.id) : state.selected.delete(x.id);
      renderCollections();
    });
    for (const doc of docs) {
      const row = document.createElement("div");
      row.className = "doc";
      const warn = doc.warnings && doc.warnings.length ? ` <span class="warn" title="${esc(doc.warnings.join("\n"))}">⚠</span>` : "";
      row.innerHTML = `<input type="checkbox" ${state.selected.has(doc.id) ? "checked" : ""}>
        <a href="#" title="${esc(doc.path)}">${esc(doc.title)}</a>${warn}`;
      $("input", row).addEventListener("change", (e) => {
        e.target.checked ? state.selected.add(doc.id) : state.selected.delete(doc.id);
        renderCollections();
      });
      $("a", row).addEventListener("click", (e) => { e.preventDefault(); showDocument(doc.id); });
      sec.append(row);
    }
    wrap.append(sec);
  }
  const s = state.info ? state.info.stats : { documents: 0 };
  $("#index-stats").textContent = state.selected.size ? `${state.selected.size} seçili` : `tümü (${s.documents})`;
}

function scope() {
  return state.selected.size ? { doc_ids: [...state.selected] } : {};
}

async function showDocument(id) {
  const d = await getJSON(`/api/documents/${id}`);
  const toc = (d.toc || []).slice(0, 400).map(([lvl, title, page]) =>
    `<li style="padding-left:${(lvl - 1) * 12}px"><a href="${fileUrl(d.id, page)}" target="_blank">${esc(title)}</a><span class="pg">s.${page}</span></li>`).join("");
  const warn = (d.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
  openTab("source");
  $("#tab-source").innerHTML = `<div class="source-card">
    <h3>${esc(d.title)}</h3>
    <div class="source-meta">${esc(d.domain)} · ${d.n_pages} sayfa · ${d.n_chunks} parça · ${esc(d.ingested_at)}</div>
    <a class="open-pdf" href="${fileUrl(d.id, 1)}" target="_blank">Dokümanı aç ↗</a>
    ${warn ? `<div class="verify warning"><ul>${warn}</ul></div>` : ""}
    ${toc ? `<h4>İçindekiler</h4><ul class="toc">${toc}</ul>` : "<p class='muted'>Bu dokümanda yer imi (outline) yok.</p>"}
  </div>`;
}

function showSource(p, activeChip) {
  openTab("source");
  const pages = p.page_start === p.page_end ? `s. ${p.page_start}` : `s. ${p.page_start}–${p.page_end}`;
  $("#tab-source").innerHTML = `<div class="source-card">
    <h3>[${p.number}] ${esc(p.doc_title)}</h3>
    <div class="source-meta">${esc(p.domain)} · ${esc(p.section || "—")} · ${pages}${p.kind === "table" ? " · tablo" : ""} · skor ${p.score.toFixed(3)}</div>
    <a class="open-pdf" href="${fileUrl(p.doc_id, p.page_start)}" target="_blank">Orijinal dokümanda aç (${pages}) ↗</a>
    <div class="source-text markdown">${renderMarkdown(p.text, false)}</div>
  </div>`;
  $$(".cite-chip.active").forEach((c) => c.classList.remove("active"));
  if (activeChip) activeChip.classList.add("active");
}

// ------------------------------------------------------------------ chat
function scrollDown() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }

function addUserMsg(q) {
  const el = document.createElement("div");
  el.className = "msg user";
  el.textContent = q;
  $("#messages").append(el);
}

function renderCites(el, sources) {
  const wrap = $(".cites", el);
  wrap.innerHTML = "";
  for (const p of sources) {
    const b = document.createElement("button");
    b.className = "cite-chip";
    b.innerHTML = `<b>[${p.number}]</b> ${esc(p.doc_title)} — ${esc(p.section ? p.section.split(" > ").pop() : "")} — s.${p.page_start}`;
    b.title = `${p.doc_title}\n${p.section}\ns. ${p.page_start}-${p.page_end}`;
    b.addEventListener("click", () => showSource(p, b));
    wrap.append(b);
  }
}

function renderVerification(el, v, confidence) {
  const box = $(".verify", el);
  if (!v) { box.hidden = true; return; }
  box.hidden = false;
  box.className = "verify " + v.status;
  const conf = confidence != null ? ` · geri getirme güveni ${(confidence * 100).toFixed(0)}%` : "";
  if (v.status === "ok") {
    box.innerHTML = `✓ Atıflar geçerli, sayısal değerler kaynaklarda bulundu${conf}`;
  } else if (v.status === "not_found") {
    box.innerHTML = `Kaynaklarda yeterli bilgi bulunamadı${conf}. Soruyu farklı terimlerle sorun veya ilgili standardı ekleyin.`;
  } else {
    const items = [];
    if (v.unsupported_numbers.length) items.push(`Kaynaklarda bulunamayan değerler: <b>${esc(v.unsupported_numbers.join(", "))}</b>`);
    if (v.invalid_citations.length) items.push(`Var olmayan kaynağa atıf: ${v.invalid_citations.map((n) => `[${n}]`).join(", ")}`);
    if (!v.citations_used.length) items.push("Cevapta hiç [n] atfı yok");
    box.innerHTML = `⚠ Doğrulama uyarısı${conf} — bu kısımları orijinal dokümandan kontrol edin:<ul>${items.map((t) => `<li>${t}</li>`).join("")}</ul>`;
  }
  if (v.uncited_sentences && v.status !== "not_found") {
    box.innerHTML += `<div class="small">${v.uncited_sentences}/${v.checked_sentences} cümlede atıf yok.</div>`;
  }
}

async function ask(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  $("#btn-send").disabled = true;
  const empty = $("#empty-state");
  if (empty) empty.remove();
  addUserMsg(question);
  const el = $("#tpl-answer").content.firstElementChild.cloneNode(true);
  $("#messages").append(el);
  scrollDown();
  const body = $(".body", el);
  let text = "";
  let sources = [];
  let confidence = null;
  let pending = false;
  const render = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; body.innerHTML = renderMarkdown(text); scrollDown(); });
  };
  el._sources = sources;

  const handle = (ev) => {
    if (ev.type === "plan") {
      const plan = ev.plan;
      const routed = ev.routed_domains && ev.routed_domains.length ? ev.routed_domains.join(", ") : "tümü";
      const parts = [];
      if (plan.english && plan.english !== plan.question) parts.push(`Arama: “${esc(plan.english)}”`);
      parts.push(`koleksiyon: ${esc(routed)}`);
      if (plan.keywords && plan.keywords.length) parts.push(`terimler: ${esc(plan.keywords.slice(0, 8).join(", "))}`);
      const p = $(".plan", el);
      p.innerHTML = parts.join(" · ");
      p.hidden = false;
    } else if (ev.type === "sources") {
      sources = ev.sources;
      confidence = ev.confidence;
      el._sources = sources;
      renderCites(el, sources);
      body.innerHTML = `<span class="typing">${sources.length} kaynak pasajı bulundu, cevap yazılıyor…</span>`;
    } else if (ev.type === "token") {
      text += ev.text;
      render();
    } else if (ev.type === "replace") {
      text = ev.text;
      render();
    } else if (ev.type === "done") {
      text = ev.answer;
      body.innerHTML = renderMarkdown(text);
      renderVerification(el, ev.verification, confidence);
      const acts = $(".msg-actions", el);
      acts.hidden = false;
      const t = ev.timings || {};
      $(".timing", acts).textContent = t.total ? `${t.total.toFixed(1)} sn` : "";
      $(".btn-note", acts).addEventListener("click", (e) => { addNote(question, text, sources); e.target.textContent = "Kaydedildi ✓"; });
      $(".btn-copy", acts).addEventListener("click", (e) => {
        const refs = sources.map((p) => `[${p.number}] ${p.doc_title} — ${p.section} — s.${p.page_start}`).join("\n");
        navigator.clipboard && navigator.clipboard.writeText(`${text}\n\nKaynaklar:\n${refs}`);
        e.target.textContent = "Kopyalandı ✓";
      });
      state.history.push({ role: "user", content: question }, { role: "assistant", content: text });
      state.history = state.history.slice(-12);
    } else if (ev.type === "error") {
      body.innerHTML = `<div class="verify warning">Hata: ${esc(ev.message)}</div>`;
    }
  };

  try {
    const res = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ question, history: state.history.slice(-6), stream: true }, scope())),
    });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("");
        if (data) handle(JSON.parse(data));
      }
    }
  } catch (e) {
    body.innerHTML = `<div class="verify warning">Hata: ${esc(e.message)}</div>`;
  } finally {
    state.busy = false;
    $("#btn-send").disabled = false;
    scrollDown();
  }
}

// ----------------------------------------------------------------- notes
function loadNotes() { try { return JSON.parse(safeGet("techrag.notes") || "[]"); } catch { return []; } }
function saveNotes() { safeSet("techrag.notes", JSON.stringify(state.notes)); renderNotes(); }

function addNote(q, a, sources) {
  state.notes.unshift({
    id: Date.now(), q, a, ts: new Date().toLocaleString(),
    sources: sources.map((p) => ({ n: p.number, title: p.doc_title, section: p.section, page: p.page_start, doc_id: p.doc_id })),
  });
  saveNotes();
}

function renderNotes() {
  $("#notes-count").textContent = state.notes.length ? `(${state.notes.length})` : "";
  const list = $("#notes-list");
  if (!state.notes.length) { list.innerHTML = `<p class="muted">Henüz not yok. Bir cevabın altındaki “Not olarak kaydet” düğmesini kullanın.</p>`; return; }
  list.innerHTML = "";
  for (const n of state.notes) {
    const div = document.createElement("div");
    div.className = "note";
    const refs = n.sources.map((s) => `<li><a href="${fileUrl(s.doc_id, s.page)}" target="_blank">[${s.n}] ${esc(s.title)}</a> — ${esc(s.section || "")} — s.${s.page}</li>`).join("");
    div.innerHTML = `<button class="ghost del">Sil</button><div class="q">${esc(n.q)}</div>
      <div class="markdown">${renderMarkdown(n.a)}</div><ul class="small">${refs}</ul><div class="small muted">${esc(n.ts)}</div>`;
    $(".del", div).addEventListener("click", () => { state.notes = state.notes.filter((x) => x.id !== n.id); saveNotes(); });
    list.append(div);
  }
}

function exportNotes() {
  const md = state.notes.map((n) => {
    const refs = n.sources.map((s) => `- [${s.n}] ${s.title} — ${s.section || ""} — s.${s.page}`).join("\n");
    return `## ${n.q}\n\n${n.a}\n\n**Kaynaklar**\n${refs}\n\n_${n.ts}_\n`;
  }).join("\n---\n\n");
  const blob = new Blob([`# Standart Asistanı — Notlar\n\n${md}`], { type: "text/markdown" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "notlar.md";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ---------------------------------------------------------------- upload
async function upload(e) {
  e.preventDefault();
  const file = $("#upload-file").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  fd.append("domain", $("#upload-domain").value);
  const log = $("#job-log");
  log.hidden = false;
  log.textContent = `${file.name} yükleniyor…\n`;
  try {
    const { job } = await (await api("/api/upload", { method: "POST", body: fd })).json();
    for (;;) {
      await new Promise((r) => setTimeout(r, 1500));
      const j = await getJSON(`/api/jobs/${job}`);
      log.textContent = j.messages.join("\n") + `\n[${j.status}]`;
      log.scrollTop = log.scrollHeight;
      if (j.status === "done" || j.status === "failed") break;
    }
    await loadInfo();
    await loadDocs();
  } catch (err) {
    log.textContent += `Hata: ${err.message}`;
  }
}

// ------------------------------------------------------------------ misc
function openTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $("#tab-source").hidden = name !== "source";
  $("#tab-notes").hidden = name !== "notes";
}

function bind() {
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#question").value.trim();
    if (!q) return;
    $("#question").value = "";
    ask(q);
  });
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $$(".example").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
  $("#messages").addEventListener("click", (e) => {
    const c = e.target.closest("button.cite");
    if (!c) return;
    const msg = c.closest(".msg");
    const p = (msg && msg._sources || []).find((s) => s.number === +c.dataset.n);
    if (p) showSource(p);
  });
  $$(".tab").forEach((t) => t.addEventListener("click", () => openTab(t.dataset.tab)));
  $("#btn-new-chat").addEventListener("click", () => {
    state.history = [];
    $("#messages").innerHTML = `<div class="empty"><p>Yeni sohbet başladı. Önceki sorular bağlam olarak kullanılmayacak.</p></div>`;
  });
  $("#btn-toggle-sources").addEventListener("click", () => $("#sources-panel").classList.toggle("open"));
  $("#btn-export-notes").addEventListener("click", exportNotes);
  $("#btn-clear-notes").addEventListener("click", () => { if (confirm("Tüm notlar silinsin mi?")) { state.notes = []; saveNotes(); } });
  $("#upload-form").addEventListener("submit", upload);
}

(async function init() {
  bind();
  renderNotes();
  try {
    await loadInfo();
    await loadDocs();
  } catch (e) {
    $("#collections").innerHTML = `<div class="verify warning">Sunucuya bağlanılamadı: ${esc(e.message)}</div>`;
  }
})();
