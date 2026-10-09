"use strict";
// TechRAG desktop UI. Offline: no external libraries, no requests except to this app's own /api.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const SERVICES = ["llm", "vision", "embedding", "reranker"];
const MASK = "••••••••";

// ------------------------------------------------------------------ i18n
const I18N = {
  tr: {
    newChat: "Yeni sohbet", library: "Kütüphane", settings: "Ayarlar", sources: "Kaynaklar",
    sourcesHint: "Seçim yapmazsanız tüm kaynaklarda aranır. Soruda adı geçen standart (DDR5, PCIe 4.0, ARINC 429 …) aramayı yalnızca o standarda kesin olarak daraltır.",
    addDocs: "Doküman ekle", collection: "Koleksiyon (klasör)", chooseFiles: "Dosya seç…", reindex: "Yeniden indeksle",
    emptyTitle: "Standartlara sorun", emptyText: "Cevaplar yalnızca yüklü dokümanlardan üretilir; her ifade [n] atfıyla kaynağına bağlanır, sayılar ve iddialar cevaptan önce kaynaklara karşı denetlenir.",
    ask: "Sor", tabSource: "Kaynak", tabNotes: "Notlar", viewerEmpty: "Bir [n] atfına veya bir dokümana tıklayın.",
    exportNotes: "Markdown kaydet", clearNotes: "Tümünü sil", answering: "Cevaplama", thinking: "Düşünme modu",
    thinkingControl: "Düşünme kontrolü", tools: "Araç kullanımı (tool calling)", judge: "Bağımsız denetçi (her iddia için ayrı LLM kontrolü)",
    regenerate: "Hatalı iddiaları bir kez yeniden ürettir", failedClaims: "Doğrulanamayan iddialar", strip: "Kaldır", flag: "İşaretle",
    visionEnabled: "VLM ile tablo çıkarımı ve sayfa görüntüleri", cancel: "Vazgeç", save: "Kaydet", close: "Kapat",
    currentLibrary: "Açık kütüphane", libraryHint: "Kütüphane klasörü: sources/<koleksiyon>/*.pdf + index.sqlite + cache/. Bir kişi indeksler, diğerleri kopyasını veya ağ paylaşımındaki yayımlanmış sürümü salt-okunur açar.",
    libraryFolder: "Kütüphane klasörü", openReadOnly: "Salt-okunur aç (paylaşılan kütüphane)", openLibrary: "Aç",
    publishTo: "Yayımlanacak hedef klasör (ör. ağ paylaşımı)", publish: "Yayımla", rebuild: "Tamamen yeniden oluştur",
    reasoning: "Model düşünmesi", saveNote: "Not olarak kaydet", copy: "Kopyala", saved: "Kaydedildi ✓", copied: "Kopyalandı ✓",
    placeholder: "Sorunuzu yazın (Türkçe veya İngilizce)… Enter: gönder, Shift+Enter: yeni satır",
    stPlanning: "Soru çözümleniyor ve kaynaklar aranıyor…", stAnswering: "Cevap yazılıyor…", stVerifying: "İfadeler kaynaklara karşı doğrulanıyor…",
    stRegenerating: "{0} ifade doğrulanamadı, düzeltiliyor…", stNoTools: "Sunucu araç çağrısını desteklemiyor; araçsız devam ediliyor.",
    scopeEntity: "kapsam: {0} (soruda geçen standart)", scopeUser: "kapsam: seçili dokümanlar", scopeDomain: "koleksiyon: {0}", scopeAll: "kapsam: tüm kütüphane",
    thinkingOn: "düşünme açık", searchAs: "arama", vOk: "✓ {0}/{1} ifade kaynaklarla doğrulandı.", vCorrected: "✓ {0}/{1} ifade doğrulandı; doğrulanamayan {2} ifade cevaptan çıkarıldı.",
    vWarning: "⚠ {0} ifade doğrulanamadı (⚠ ile işaretli) — orijinal sayfadan kontrol edin.", vNotFound: "Yüklü dokümanlarda bu sorunun cevabı bulunamadı.",
    vRegenerated: "Cevap bir kez düzeltilerek yeniden üretildi.", vJudge: "Sayısal kontrol + bağımsız denetçi", vDet: "Sayısal/atıf kontrolü",
    removedList: "Çıkarılan ifadeler", prev: "‹ Önceki", next: "Sonraki ›", zoom: "Yakınlaştır", openOriginal: "Orijinal dosyayı aç",
    page: "Sayfa", pages: "sayfa", chunks: "parça", superseded: "eski revizyon", toc: "İçindekiler", noToc: "Bu dokümanda yer imi yok.",
    readOnly: "salt-okunur", docs: "doküman", params: "parametre", modelsLoad: "Modelleri getir", test: "Test et",
    sameAsChat: "Sohbet modeliyle aynı", baseUrl: "API adresi (…/v1)", apiKey: "API anahtarı", model: "Model",
    svc_llm: "Sohbet modeli (LLM)", svc_vision: "Görsel model (VLM)", svc_embedding: "Embedding", svc_reranker: "Reranker",
    enabled: "etkin", notOnServer: "(sunucuda yok)", savedSettings: "Ayarlar kaydedildi.", noModels: "Model listesi alınamadı",
    errorPrefix: "Hata", needToken: "Bu sunucu bir API anahtarı istiyor:", confirmClear: "Tüm notlar silinsin mi?",
    noNotes: "Henüz not yok.", newChatStarted: "Yeni sohbet başladı.", calc: "hesap", parameter: "parametre", table: "tablo", pageKind: "sayfa", unverified: "doğrulanmamış çıkarım",
    tlsTitle: "Bağlantı güvenliği (HTTPS sertifikaları)",
    tlsSystem: "Windows sertifika deposuna güven (BT'nin dağıttığı şirket CA sertifikaları)",
    tlsProxy: "Sistem proxy ayarlarını kullan (model sunucuları yerel ağdaysa kapalı bırakın)",
    tlsCa: "Ek CA / sunucu sertifika dosyaları (.pem, .crt, .cer — birden fazlaysa ; ile ayırın)",
    tlsHint: "Bu bölümdeki değişiklikler hemen uygulanır. 'Bu sunucuya güven' ile kaydedilen sertifikalar da bu listeye eklenir.",
    tlsApplied: "TLS ayarları uygulandı.", insecure: "SSL doğrulamasını kapat (güvensiz — yalnızca test için)",
    trustServer: "Bu sunucuya güven…", trust: "Güven", certTitle: "Sunucunun sunduğu sertifika",
    certSubject: "Konu", certIssuer: "Veren", certValid: "Geçerlilik", certFp: "SHA-256 parmak izi", certNames: "Adlar",
    certSelf: "kendinden imzalı", certCheck: "Bu parmak izini sunucu yöneticisinden doğrulayın; aynıysa güvenin.",
    certAlreadyOk: "Mevcut ayarlarla bu sertifika zaten doğrulanıyor.", trusted: "Sertifika kaydedildi ve güvenilenlere eklendi: {0}",
    details: "Ayrıntı",
    err_tls_untrusted: "Sunucu sertifikasına güvenilmiyor (kendinden imzalı ya da bu bilgisayarın tanımadığı bir şirket CA'sı). Şirket kök CA'sını Windows'a yükletin, CA dosyasını aşağıdaki TLS bölümüne ekleyin ya da parmak izini kontrol edip 'Bu sunucuya güven'i kullanın.",
    err_tls_hostname: "Sertifika başka bir ad/IP için verilmiş. API adresinde sertifikadaki adı kullanın (ör. IP yerine https://vllm.sirket.local:8000/v1).",
    err_tls_expired: "Sunucu sertifikasının süresi dolmuş ya da henüz geçerli değil (bu bilgisayarın saatini de kontrol edin).",
    err_tls_protocol: "TLS el sıkışması başarısız: sunucu büyük olasılıkla düz HTTP konuşuyor. https:// yerine http:// deneyin.",
    err_tls_other: "TLS bağlantısı kurulamadı.", err_connect: "Bağlanılamadı: adresi/portu, sunucunun çalıştığını ve güvenlik duvarını kontrol edin.",
    err_model: "Seçilen model bu sunucuda yok.",
    published: "Yayımlandı: {0}", opened: "Kütüphane açıldı.", pickFolderPrompt: "Klasör yolu:",
    ex: ["DDR4 ile DDR5 arasında VDD ve burst length farkları nelerdir?", "PCIe 5.0 LTSSM Polling alt durumları ve geçiş koşulları nelerdir?",
         "ARINC 429 kelimesinde SSM bitleri hangi bitlerdir ve BNR verisi için anlamları nedir?", "I2C Fast-mode Plus için maksimum yükselme süresi ve bus kapasitansı nedir?"],
  },
  en: {
    newChat: "New chat", library: "Library", settings: "Settings", sources: "Sources",
    sourcesHint: "With nothing selected, all sources are searched. A standard named in the question (DDR5, PCIe 4.0, ARINC 429 …) hard-restricts the search to that standard.",
    addDocs: "Add documents", collection: "Collection (folder)", chooseFiles: "Choose files…", reindex: "Re-index",
    emptyTitle: "Ask the standards", emptyText: "Answers come only from the loaded documents; every statement is tied to its source with an [n] citation, and numbers and claims are checked against the sources before you see the answer.",
    ask: "Ask", tabSource: "Source", tabNotes: "Notes", viewerEmpty: "Click an [n] citation or a document.",
    exportNotes: "Save as Markdown", clearNotes: "Delete all", answering: "Answering", thinking: "Thinking mode",
    thinkingControl: "Thinking control", tools: "Tool calling", judge: "Independent judge (separate LLM check per claim)",
    regenerate: "Regenerate failing claims once", failedClaims: "Unverifiable claims", strip: "Remove", flag: "Flag",
    visionEnabled: "VLM table extraction and page images", cancel: "Cancel", save: "Save", close: "Close",
    currentLibrary: "Open library", libraryHint: "A library folder holds sources/<collection>/*.pdf + index.sqlite + cache/. One person indexes; others open a copy or a published version on a network share read-only.",
    libraryFolder: "Library folder", openReadOnly: "Open read-only (shared library)", openLibrary: "Open",
    publishTo: "Publish to folder (e.g. a network share)", publish: "Publish", rebuild: "Full rebuild",
    reasoning: "Model reasoning", saveNote: "Save as note", copy: "Copy", saved: "Saved ✓", copied: "Copied ✓",
    placeholder: "Type your question (English or Turkish)… Enter: send, Shift+Enter: new line",
    stPlanning: "Analysing the question and searching…", stAnswering: "Writing the answer…", stVerifying: "Checking statements against the sources…",
    stRegenerating: "{0} statement(s) failed verification, correcting…", stNoTools: "The server does not support tool calls; continuing without tools.",
    scopeEntity: "scope: {0} (named in the question)", scopeUser: "scope: selected documents", scopeDomain: "collection: {0}", scopeAll: "scope: whole library",
    thinkingOn: "thinking on", searchAs: "search", vOk: "✓ {0}/{1} statements verified against the sources.", vCorrected: "✓ {0}/{1} statements verified; {2} unverifiable statement(s) removed.",
    vWarning: "⚠ {0} statement(s) could not be verified (marked ⚠) — check the original page.", vNotFound: "The loaded documents do not answer this question.",
    vRegenerated: "The answer was corrected and regenerated once.", vJudge: "Numeric check + independent judge", vDet: "Numeric/citation check",
    removedList: "Removed statements", prev: "‹ Prev", next: "Next ›", zoom: "Zoom", openOriginal: "Open original file",
    page: "Page", pages: "pages", chunks: "chunks", superseded: "superseded", toc: "Contents", noToc: "This document has no outline.",
    readOnly: "read-only", docs: "documents", params: "parameters", modelsLoad: "Load models", test: "Test",
    sameAsChat: "Same as chat model", baseUrl: "API base URL (…/v1)", apiKey: "API key", model: "Model",
    svc_llm: "Chat model (LLM)", svc_vision: "Vision model (VLM)", svc_embedding: "Embedding", svc_reranker: "Reranker",
    enabled: "enabled", notOnServer: "(not on server)", savedSettings: "Settings saved.", noModels: "Could not load models",
    errorPrefix: "Error", needToken: "This server requires an API token:", confirmClear: "Delete all notes?",
    noNotes: "No notes yet.", newChatStarted: "New chat started.", calc: "calculation", parameter: "parameter", table: "table", pageKind: "page", unverified: "unverified extraction",
    tlsTitle: "Connection security (HTTPS certificates)",
    tlsSystem: "Trust the Windows certificate store (company CA certificates deployed by IT)",
    tlsProxy: "Use system proxy settings (leave off when the model servers are on the LAN)",
    tlsCa: "Extra CA / server certificate files (.pem, .crt, .cer — separate several with ;)",
    tlsHint: "Changes in this section apply immediately. Certificates saved with 'Trust this server' are added to this list.",
    tlsApplied: "TLS settings applied.", insecure: "Disable SSL verification (insecure — testing only)",
    trustServer: "Trust this server…", trust: "Trust", certTitle: "Certificate presented by the server",
    certSubject: "Subject", certIssuer: "Issuer", certValid: "Valid", certFp: "SHA-256 fingerprint", certNames: "Names",
    certSelf: "self-signed", certCheck: "Confirm this fingerprint with the server administrator; trust it only if it matches.",
    certAlreadyOk: "This certificate already verifies with the current settings.", trusted: "Certificate saved and trusted: {0}",
    details: "Details",
    err_tls_untrusted: "The server certificate is not trusted (self-signed, or issued by a company CA this PC does not know). Have IT install the company root CA, add the CA file in the TLS section below, or check the fingerprint and use 'Trust this server'.",
    err_tls_hostname: "The certificate was issued for a different name/IP. Use the name in the certificate in the base URL (e.g. https://vllm.company.local:8000/v1 instead of the IP).",
    err_tls_expired: "The server certificate has expired or is not yet valid (also check this PC's clock).",
    err_tls_protocol: "TLS handshake failed: the server probably speaks plain HTTP. Try http:// instead of https://.",
    err_tls_other: "TLS connection failed.", err_connect: "Cannot connect: check the address/port, that the server is running and the firewall.",
    err_model: "The selected model is not served here.",
    published: "Published: {0}", opened: "Library opened.", pickFolderPrompt: "Folder path:",
    ex: ["What are the VDD and burst length differences between DDR4 and DDR5?", "What are the PCIe 5.0 LTSSM Polling substates and their exit conditions?",
         "Which bits are the SSM in an ARINC 429 word and what do they mean for BNR data?", "What is the maximum rise time and bus capacitance for I2C Fast-mode Plus?"],
  },
};
let LANG = "tr";
function t(key, ...args) {
  let s = (I18N[LANG] && I18N[LANG][key]) ?? I18N.en[key] ?? key;
  args.forEach((a, i) => { s = String(s).replace(`{${i}}`, a); });
  return s;
}
function applyI18n() {
  document.documentElement.lang = LANG;
  $$("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $("#question").placeholder = t("placeholder");
  $$(".lang").forEach((b) => b.classList.toggle("active", b.dataset.lang === LANG));
  const ex = $("#examples");
  if (ex) {
    ex.innerHTML = "";
    for (const q of t("ex")) {
      const b = document.createElement("button");
      b.className = "example";
      b.textContent = q;
      b.addEventListener("click", () => ask(q));
      ex.append(b);
    }
  }
}

// ------------------------------------------------------------------ state + api
const state = { info: null, docs: [], selected: new Set(), history: [], busy: false, notes: loadNotes(), token: TOKEN };

function safeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function safeSet(k, v) { try { localStorage.setItem(k, v); } catch { /* unavailable */ } }
const desk = () => (window.pywebview && window.pywebview.api) || null;

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers.Authorization = "Bearer " + state.token;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401 && !TOKEN) {
    const tok = prompt(t("needToken"));
    if (tok) { state.token = tok.trim(); return api(path, opts); }
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return res;
}
const getJSON = async (p) => (await api(p)).json();
const postJSON = async (p, body, method = "POST") =>
  (await api(p, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const tokenQ = () => (state.token ? `?token=${encodeURIComponent(state.token)}` : "");
const pageUrl = (docId, page, dpi) => `/api/documents/${docId}/page/${page}.png${tokenQ()}${dpi ? (tokenQ() ? "&" : "?") + "dpi=" + dpi : ""}`;

// ------------------------------------------------------------------ markdown
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
function inline(s, cites) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s.,;:)]|$)/g, "$1<em>$2</em>");
  if (cites) {
    s = s.replace(/\[(\d+(?:\s*[,–-]\s*\d+)*)\]/g, (m, inner) =>
      expandCites(inner).map((n) => `<button class="cite" data-n="${n}">${n}</button>`).join(""));
    s = s.replace(/⚠/g, '<span class="flag">⚠</span>');
  }
  return s.replace(/\u0000(\d+)\u0000/g, (m, i) => `<code>${codes[+i]}</code>`);
}
const LIST_RE = /^\s*([-*•]|\d+[.)])\s+/;
function renderTable(rows, cites) {
  const cells = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
  if (!body.length) return "";
  let h = "<table><thead><tr>" + cells(body[0]).map((c) => `<th>${inline(c, cites)}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of body.slice(1)) h += "<tr>" + cells(r).map((c) => `<td>${inline(c, cites)}</td>`).join("") + "</tr>";
  return h + "</tbody></table>";
}
function renderMarkdown(md, cites = true) {
  const lines = esc(md).split("\n");
  const inl = (x) => inline(x, cites);
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
    if (h) { const lvl = Math.min(h[1].length + 1, 4); html += `<h${lvl}>${inl(h[2])}</h${lvl}>`; i++; continue; }
    if (LIST_RE.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items = [];
      while (i < lines.length && LIST_RE.test(lines[i])) {
        items.push(lines[i].replace(LIST_RE, ""));
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !LIST_RE.test(lines[i])) { items[items.length - 1] += " " + lines[i].trim(); i++; }
      }
      const tag = ordered ? "ol" : "ul";
      html += `<${tag}>${items.map((x) => `<li>${inl(x)}</li>`).join("")}</${tag}>`;
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !/^(\s*```|\s*\||#{1,6}\s)/.test(lines[i]) && !LIST_RE.test(lines[i])) para.push(lines[i++]);
    html += `<p>${inl(para.join("<br>"))}</p>`;
  }
  return html;
}

// ------------------------------------------------------------------ info + sources panel
async function loadInfo() {
  state.info = await getJSON("/api/info");
  const i = state.info;
  if (!safeGet("techrag.lang")) LANG = i.language || LANG;
  const ro = i.read_only ? ` · ${t("readOnly")}` : "";
  $("#lib-info").textContent = `${i.library} · ${i.stats.documents} ${t("docs")} · ${i.stats.parameters || 0} ${t("params")}${ro}`;
  $("#add-box").hidden = !i.allow_upload;
  const banner = $("#banner");
  const missing = SERVICES.filter((s) => s !== "vision" && !i.models[s]);
  if (i.error) { banner.hidden = false; banner.textContent = `${t("errorPrefix")}: ${i.error}`; }
  else if (missing.length) { banner.hidden = false; banner.textContent = `${t("settings")}: ${missing.map((s) => t("svc_" + s)).join(", ")} — ${t("model")}?`; }
  else banner.hidden = true;
  $("#collection-list").innerHTML = i.domains.map((d) => `<option value="${esc(d.key)}">${esc(d.name)}</option>`).join("");
}

async function loadDocs() {
  try { state.docs = await getJSON("/api/documents"); } catch { state.docs = []; }
  const ids = new Set(state.docs.map((d) => d.id));
  for (const id of [...state.selected]) if (!ids.has(id)) state.selected.delete(id);
  renderCollections();
}

function docTags(d) {
  const tags = (d.entities || []).slice(0, 2).map((e) => `<span class="tag">${esc(e)}</span>`);
  if (d.doc_type && d.doc_type !== "base") tags.push(`<span class="tag errata">${esc(d.doc_type.toUpperCase())}</span>`);
  if (d.revision) tags.push(`<span class="tag">rev ${esc(d.revision)}</span>`);
  if (d.superseded_by) tags.push(`<span class="tag">${t("superseded")}</span>`);
  return tags.join("");
}

function renderCollections() {
  const wrap = $("#collections");
  wrap.innerHTML = "";
  const domains = state.info ? state.info.domains.filter((d) => d.documents > 0) : [];
  for (const d of domains) {
    const docs = state.docs.filter((x) => x.domain === d.key);
    const sec = document.createElement("div");
    sec.className = "collection";
    const allSel = docs.length > 0 && docs.every((x) => state.selected.has(x.id));
    const someSel = docs.some((x) => state.selected.has(x.id));
    sec.innerHTML = `<label><input type="checkbox" ${allSel ? "checked" : ""}><span title="${esc(d.name)}">${esc(d.key.toUpperCase())}</span><span class="count">${docs.length}</span></label>`;
    const cb = $("input", sec);
    cb.indeterminate = someSel && !allSel;
    cb.addEventListener("change", () => { for (const x of docs) cb.checked ? state.selected.add(x.id) : state.selected.delete(x.id); renderCollections(); });
    for (const doc of docs) {
      const row = document.createElement("div");
      row.className = "doc" + (doc.superseded_by ? " superseded" : "");
      const warn = doc.warnings && doc.warnings.length ? ` <span class="tag errata" title="${esc(doc.warnings.join("\n"))}">⚠</span>` : "";
      row.innerHTML = `<input type="checkbox" ${state.selected.has(doc.id) ? "checked" : ""}><span><a href="#" title="${esc(doc.path)}">${esc(doc.title)}</a> ${docTags(doc)}${warn}</span>`;
      $("input", row).addEventListener("change", (e) => { e.target.checked ? state.selected.add(doc.id) : state.selected.delete(doc.id); renderCollections(); });
      $("a", row).addEventListener("click", (e) => { e.preventDefault(); showDocument(doc.id); });
      sec.append(row);
    }
    wrap.append(sec);
  }
  $("#scope-info").textContent = state.selected.size ? `${state.selected.size} ✓` : "";
}

const scope = () => (state.selected.size ? { doc_ids: [...state.selected] } : {});

// ------------------------------------------------------------------ viewer
function openTab(name) {
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $("#tab-source").hidden = name !== "source";
  $("#tab-notes").hidden = name !== "notes";
}

function pageViewer(container, docId, page, nPages) {
  let p = page;
  const box = document.createElement("div");
  box.innerHTML = `<div class="page-nav"><button type="button" class="ghost small pv-prev">${t("prev")}</button>
    <span class="pv-label"></span><button type="button" class="ghost small pv-next">${t("next")}</button>
    <button type="button" class="ghost small pv-zoom">${t("zoom")}</button>
    ${state.info && state.info.desktop ? `<button type="button" class="ghost small pv-open">${t("openOriginal")}</button>` : ""}</div>
    <div class="page-view"><img alt=""></div>`;
  const img = $("img", box);
  const show = () => {
    img.src = pageUrl(docId, p, 130);
    $(".pv-label", box).textContent = `${t("page")} ${p}${nPages ? " / " + nPages : ""}`;
    $(".pv-prev", box).disabled = p <= 1;
    $(".pv-next", box).disabled = nPages ? p >= nPages : false;
  };
  $(".pv-prev", box).addEventListener("click", () => { if (p > 1) { p--; show(); } });
  $(".pv-next", box).addEventListener("click", () => { p++; show(); });
  $(".pv-zoom", box).addEventListener("click", () => $(".page-view", box).classList.toggle("zoom"));
  const open = $(".pv-open", box);
  if (open) open.addEventListener("click", () => postJSON(`/api/documents/${docId}/open`, {}).catch((e) => alert(e.message)));
  container.append(box);
  show();
}

function kindLabel(s) {
  return { parameter: t("parameter"), table: t("table"), page: t("pageKind"), calc: t("calc") }[s.kind] || "";
}

function showSource(s, chip) {
  openTab("source");
  const pane = $("#tab-source");
  const pages = s.page_start === s.page_end ? `${t("page")} ${s.page_start}` : `${t("page")} ${s.page_start}–${s.page_end}`;
  const meta = [(s.entities || []).join(", "), s.doc_type && s.doc_type !== "base" ? s.doc_type.toUpperCase() : "", s.section, s.doc_id ? pages : "",
    kindLabel(s), s.verified === false ? t("unverified") : ""].filter(Boolean).map(esc).join(" · ");
  pane.innerHTML = `<div class="source-card"><h3>[${s.n}] ${esc(s.kind === "calc" ? t("calc") : s.doc_title)}${s.revision ? ` <span class="tag">rev ${esc(s.revision)}</span>` : ""}</h3>
    <div class="source-meta">${meta}</div><div class="pv"></div><div class="source-text markdown">${renderMarkdown(s.text, false)}</div></div>`;
  if (s.doc_id) {
    const doc = state.docs.find((d) => d.id === s.doc_id);
    pageViewer($(".pv", pane), s.doc_id, s.page_start || 1, doc ? doc.n_pages : 0);
  }
  $$(".cite-chip.active").forEach((c) => c.classList.remove("active"));
  if (chip) chip.classList.add("active");
}

async function showDocument(id) {
  const d = await getJSON(`/api/documents/${id}`);
  openTab("source");
  const pane = $("#tab-source");
  const toc = (d.toc || []).slice(0, 500).map(([lvl, title, page]) =>
    `<li style="padding-left:${(lvl - 1) * 12}px"><a data-page="${page}">${esc(title)}</a><span class="pg">${page}</span></li>`).join("");
  const warn = (d.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
  pane.innerHTML = `<div class="source-card"><h3>${esc(d.title)} ${docTags(d)}</h3>
    <div class="source-meta">${esc(d.domain)} · ${d.n_pages} ${t("pages")} · ${d.n_chunks} ${t("chunks")} · ${esc(d.doc_date || "")}</div>
    ${warn ? `<div class="verify warning"><ul>${warn}</ul></div>` : ""}<div class="pv"></div>
    <h3 style="margin-top:10px">${t("toc")}</h3>${toc ? `<ul class="toc">${toc}</ul>` : `<p class="muted">${t("noToc")}</p>`}</div>`;
  const pv = $(".pv", pane);
  pageViewer(pv, d.id, 1, d.n_pages);
  $$(".toc a", pane).forEach((a) => a.addEventListener("click", () => { pv.innerHTML = ""; pageViewer(pv, d.id, +a.dataset.page, d.n_pages); }));
}

// ------------------------------------------------------------------ chat
const scrollDown = () => { const m = $("#messages"); m.scrollTop = m.scrollHeight; };

function renderChips(el) {
  const wrap = $(".cites", el);
  wrap.innerHTML = "";
  const list = [...el._sources].sort((a, b) => (b.cited === true) - (a.cited === true) || a.n - b.n);
  for (const s of list) {
    if (el._final && s.cited === false && list.some((x) => x.cited)) continue;
    const b = document.createElement("button");
    b.className = "cite-chip" + (s.cited === false ? " uncited" : "");
    const where = s.kind === "calc" ? s.text : `${s.doc_title}${s.section ? " — " + s.section.split(" > ").pop() : ""} — ${t("page")} ${s.page_start}`;
    b.innerHTML = `<b>[${s.n}]</b> ${kindLabel(s) ? `<i>${esc(kindLabel(s))}</i> ` : ""}${esc(where)}`;
    b.title = s.text.slice(0, 400);
    b.addEventListener("click", () => showSource(s, b));
    wrap.append(b);
  }
}

function renderVerification(el, v, confidence) {
  const box = $(".verify", el);
  if (!v) { box.hidden = true; return; }
  box.hidden = false;
  box.className = "verify " + v.status;
  let html = "";
  if (v.status === "ok") html = t("vOk", v.supported, v.checked);
  else if (v.status === "corrected") html = t("vCorrected", v.supported, v.checked, v.removed.length);
  else if (v.status === "not_found") html = t("vNotFound");
  else html = t("vWarning", (v.flagged || []).length || v.failed);
  const notes = [v.judge_used ? t("vJudge") : t("vDet")];
  if (v.regenerated) notes.push(t("vRegenerated"));
  if (confidence != null) notes.push(`rerank ${(confidence * 100).toFixed(0)}%`);
  html += `<div class="small">${notes.map(esc).join(" · ")}</div>`;
  const failed = (v.details || []).filter((c) => c.status === "removed" || c.status === "fail");
  if (failed.length) {
    html += `<details><summary>${t("removedList")} (${failed.length})</summary><ul>${failed.map((c) =>
      `<li>${esc(c.text)}<br><span class="small">${esc((c.reasons || []).join("; "))}</span></li>`).join("")}</ul></details>`;
  }
  box.innerHTML = html;
}

function stageText(ev) {
  return { planning: t("stPlanning"), answering: t("stAnswering"), verifying: t("stVerifying"),
    regenerating: t("stRegenerating", ev.failed || ""), tools_unsupported: t("stNoTools") }[ev.stage] || "";
}

async function ask(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  $("#btn-send").disabled = true;
  const empty = $("#empty-state");
  if (empty) empty.remove();
  const u = document.createElement("div");
  u.className = "msg user";
  u.textContent = question;
  $("#messages").append(u);
  const el = $("#tpl-answer").content.firstElementChild.cloneNode(true);
  $$("[data-i18n]", el).forEach((x) => { x.textContent = t(x.dataset.i18n); });
  $("#messages").append(el);
  el._sources = [];
  el._final = false;
  const body = $(".body", el);
  const status = $(".status", el);
  let text = "";
  let confidence = null;
  let pending = false;
  const render = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; body.innerHTML = renderMarkdown(text); scrollDown(); });
  };
  scrollDown();

  const handle = (ev) => {
    switch (ev.type) {
      case "status": status.textContent = stageText(ev); break;
      case "plan": {
        const s = ev.scope || {};
        const parts = [];
        if (s.reason === "entity") parts.push(t("scopeEntity", (s.entities || []).join(", ")));
        else if (s.reason === "user") parts.push(t("scopeUser"));
        else if (s.reason === "domain") parts.push(t("scopeDomain", (s.domains || []).join(", ")));
        else parts.push(t("scopeAll"));
        if (ev.plan.english && ev.plan.english !== ev.plan.question) parts.push(`${t("searchAs")}: “${esc(ev.plan.english)}”`);
        if (ev.thinking) parts.push(t("thinkingOn"));
        const p = $(".plan", el);
        p.innerHTML = parts.join(" · ");
        p.hidden = false;
        break;
      }
      case "sources": el._sources = ev.sources; confidence = ev.confidence; renderChips(el); break;
      case "sources_add": el._sources = el._sources.concat(ev.sources); renderChips(el); break;
      case "reasoning": { const d = $(".reasoning", el); d.hidden = false; $("pre", d).textContent += ev.text; break; }
      case "tool": {
        const ul = $(".tool-log", el);
        ul.hidden = false;
        const li = document.createElement("li");
        li.textContent = `${ev.name}: ${ev.summary}`;
        ul.append(li);
        break;
      }
      case "draft": body.classList.add("draft"); text += ev.text; render(); break;
      case "draft_reset": text = ""; render(); break;
      case "final": {
        el._final = true;
        status.textContent = "";
        body.classList.remove("draft");
        text = ev.answer;
        body.innerHTML = renderMarkdown(text);
        el._sources = ev.sources || el._sources;
        renderChips(el);
        renderVerification(el, ev.verification, confidence);
        const acts = $(".msg-actions", el);
        acts.hidden = false;
        $(".timing", acts).textContent = ev.timings && ev.timings.total ? `${ev.timings.total.toFixed(1)} s` : "";
        $(".btn-note", acts).addEventListener("click", (e) => { addNote(question, text, el._sources); e.target.textContent = t("saved"); });
        $(".btn-copy", acts).addEventListener("click", (e) => {
          const refs = el._sources.filter((s) => s.cited).map((s) => `[${s.n}] ${s.doc_title} — ${s.section} — p.${s.page_start}`).join("\n");
          navigator.clipboard && navigator.clipboard.writeText(`${text}\n\n${refs}`);
          e.target.textContent = t("copied");
        });
        state.history.push({ role: "user", content: question }, { role: "assistant", content: text });
        state.history = state.history.slice(-12);
        break;
      }
      case "error": status.textContent = ""; body.innerHTML = `<div class="verify warning">${t("errorPrefix")}: ${esc(ev.message)}</div>`; break;
      default: break;
    }
  };

  try {
    const res = await api("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ question, history: state.history.slice(-6), stream: true }, scope())) });
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
    status.textContent = "";
    body.innerHTML = `<div class="verify warning">${t("errorPrefix")}: ${esc(e.message)}</div>`;
  } finally {
    state.busy = false;
    $("#btn-send").disabled = false;
    scrollDown();
  }
}

// ------------------------------------------------------------------ notes
function loadNotes() { try { return JSON.parse(safeGet("techrag.notes") || "[]"); } catch { return []; } }
function saveNotes() { safeSet("techrag.notes", JSON.stringify(state.notes)); renderNotes(); }
function addNote(q, a, sources) {
  state.notes.unshift({ id: Date.now(), q, a, ts: new Date().toLocaleString(),
    sources: sources.filter((s) => s.cited !== false).map((s) => ({ n: s.n, title: s.doc_title, section: s.section, page: s.page_start })) });
  saveNotes();
}
function renderNotes() {
  $("#notes-count").textContent = state.notes.length ? `(${state.notes.length})` : "";
  const list = $("#notes-list");
  if (!state.notes.length) { list.innerHTML = `<p class="muted">${t("noNotes")}</p>`; return; }
  list.innerHTML = "";
  for (const n of state.notes) {
    const div = document.createElement("div");
    div.className = "note";
    div.innerHTML = `<button class="ghost del">✕</button><div class="q">${esc(n.q)}</div><div class="markdown">${renderMarkdown(n.a, false)}</div>
      <ul class="small">${n.sources.map((s) => `<li>[${s.n}] ${esc(s.title)} — ${esc(s.section || "")} — p.${s.page}</li>`).join("")}</ul><div class="small muted">${esc(n.ts)}</div>`;
    $(".del", div).addEventListener("click", () => { state.notes = state.notes.filter((x) => x.id !== n.id); saveNotes(); });
    list.append(div);
  }
}
async function exportNotes() {
  const md = "# TechRAG\n\n" + state.notes.map((n) =>
    `## ${n.q}\n\n${n.a}\n\n${n.sources.map((s) => `- [${s.n}] ${s.title} — ${s.section || ""} — p.${s.page}`).join("\n")}\n\n_${n.ts}_\n`).join("\n---\n\n");
  if (desk()) { await desk().save_text("techrag-notes.md", md); return; }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([md], { type: "text/markdown" }));
  a.download = "techrag-notes.md";
  a.click();
}

// ------------------------------------------------------------------ jobs / adding documents
async function pollJob(job, log) {
  log.hidden = false;
  for (;;) {
    await new Promise((r) => setTimeout(r, 1200));
    const j = await getJSON(`/api/jobs/${job}`);
    log.textContent = j.messages.slice(-200).join("\n") + `\n[${j.status}]`;
    log.scrollTop = log.scrollHeight;
    if (j.status === "done" || j.status === "failed") break;
  }
  await loadInfo();
  await loadDocs();
}
async function addFiles() {
  const collection = $("#add-collection").value.trim() || "general";
  const log = $("#job-log");
  try {
    if (desk()) {
      const paths = await desk().pick_files();
      if (!paths || !paths.length) return;
      const r = await postJSON("/api/sources/add", { paths, collection });
      await pollJob(r.job, log);
    } else {
      $("#add-file-input").click();
    }
  } catch (e) { log.hidden = false; log.textContent = `${t("errorPrefix")}: ${e.message}`; }
}
async function uploadFiles(files) {
  const collection = $("#add-collection").value.trim() || "general";
  const log = $("#job-log");
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("domain", collection);
    try { const r = await (await api("/api/upload", { method: "POST", body: fd })).json(); await pollJob(r.job, log); }
    catch (e) { log.hidden = false; log.textContent += `\n${t("errorPrefix")}: ${e.message}`; }
  }
}
async function reindex(rebuild) {
  try { const r = await postJSON("/api/ingest", { rebuild: !!rebuild }); await pollJob(r.job, $("#job-log")); }
  catch (e) { alert(e.message); }
}

// ------------------------------------------------------------------ settings dialog
let SETTINGS = null;
function svcCard(svc) {
  const div = document.createElement("div");
  div.className = "svc";
  div.dataset.svc = svc;
  const s = SETTINGS[svc];
  const extra = svc === "vision" ? `<label><input type="checkbox" class="same"> ${t("sameAsChat")}</label>`
    : svc === "reranker" ? `<label><input type="checkbox" class="enabled"> ${t("enabled")}</label>` : "";
  div.innerHTML = `<h3>${t("svc_" + svc)}</h3>${extra}
    <label>${t("baseUrl")}<input class="url" value="${esc(s.base_url)}" placeholder="http://server:8000/v1"></label>
    <label>${t("apiKey")}<input class="key" type="password" value="${esc(s.api_key)}" autocomplete="off"></label>
    <label>${t("model")}<div class="row"><select class="model"></select><button type="button" class="ghost load">${t("modelsLoad")}</button><button type="button" class="ghost test">${t("test")}</button></div></label>
    <label class="insecure-row"><input type="checkbox" class="insecure"> ${t("insecure")}</label>
    <div class="result"></div>`;
  $(".insecure", div).checked = s.verify_ssl === false;
  const same = $(".same", div);
  if (same) {
    same.checked = !s.base_url && !s.model;
    const sync = () => $$(".url,.key,.model,.load,.test,.insecure", div).forEach((x) => { x.disabled = same.checked; });
    same.addEventListener("change", sync);
    sync();
  }
  const en = $(".enabled", div);
  if (en) en.checked = !!s.enabled;
  const sel = $(".model", div);
  const setOptions = (models) => {
    const cur = s.model;
    const opts = [...new Set([...(models || []), ...(cur ? [cur] : [])])];
    sel.innerHTML = `<option value=""></option>` + opts.map((m) =>
      `<option value="${esc(m)}" ${m === cur ? "selected" : ""}>${esc(m)}${models && !models.includes(m) ? " " + t("notOnServer") : ""}</option>`).join("");
  };
  setOptions(null);
  const load = async () => {
    const res = $(".result", div);
    res.className = "result";
    res.textContent = "…";
    try {
      const r = await postJSON("/api/settings/models", probe());
      s.model = sel.value || s.model;
      if (r.ok) { setOptions(r.models); res.textContent = `${r.models.length} model`; }
      else showError(res, r, t("noModels"));
    } catch (e) { res.className = "result bad"; res.textContent = e.message; }
  };
  const probe = (extra) => Object.assign({ service: svc, base_url: $(".url", div).value, api_key: $(".key", div).value,
    verify_ssl: !$(".insecure", div).checked }, extra || {});
  const showError = (res, r, prefix) => {
    res.className = "result bad";
    const hint = I18N[LANG]["err_" + r.code] || I18N.en["err_" + r.code];
    res.innerHTML = `<div class="hint-text">✕ ${esc(hint || prefix || "")}</div>` +
      (r.error ? `<details><summary>${t("details")}</summary>${esc(r.error)}</details>` : "");
    if (r.code === "tls_untrusted" && /^https:/i.test($(".url", div).value.trim())) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ghost small";
      b.textContent = t("trustServer");
      b.addEventListener("click", () => trustFlow(res));
      res.append(b);
    }
  };
  const trustFlow = async (res) => {
    const base = $(".url", div).value.trim();
    const info = await postJSON("/api/settings/certificate", { base_url: base });
    if (!info.ok) { showError(res, info); return; }
    const leaf = info.chain[0];
    const panel = document.createElement("div");
    panel.className = "cert-panel";
    const rows = [[t("certSubject"), leaf.subject + (leaf.self_signed ? ` (${t("certSelf")})` : "")], [t("certIssuer"), leaf.issuer],
      [t("certNames"), (leaf.names || []).join(", ")], [t("certValid"), `${leaf.not_before} → ${leaf.not_after}`]];
    panel.innerHTML = `<b>${t("certTitle")}</b> — ${esc(info.host)}:${info.port}
      <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v || "—")}</dd>`).join("")}
      <dt>${t("certFp")}</dt><dd><code>${esc(leaf.sha256)}</code></dd></dl>
      <div>${info.verify.ok ? t("certAlreadyOk") : t("certCheck")}</div>
      <div class="row" style="margin-top:6px"><button type="button" class="do-trust">${t("trust")}</button><button type="button" class="ghost no-trust">${t("cancel")}</button></div>`;
    $(".no-trust", panel).addEventListener("click", () => panel.remove());
    $(".do-trust", panel).addEventListener("click", async () => {
      try {
        const r = await postJSON("/api/settings/trust", { base_url: base, sha256: leaf.sha256 });
        SETTINGS.tls = r.settings.tls;
        $("#set-tls-ca").value = SETTINGS.tls.ca_bundle;
        panel.remove();
        $("#settings-status").textContent = t("trusted", r.path);
        await load();
      } catch (e) { panel.innerHTML = `<span class="hint-text">${esc(e.message)}</span>`; }
    });
    res.append(panel);
  };
  $(".load", div).addEventListener("click", load);
  $(".insecure", div).addEventListener("change", load);
  $(".url", div).addEventListener("change", load);
  $(".key", div).addEventListener("change", load);
  $(".test", div).addEventListener("click", async () => {
    const res = $(".result", div);
    res.className = "result";
    res.textContent = "…";
    const r = await postJSON("/api/settings/test", probe({ model: sel.value }));
    if (r.ok) { res.className = "result ok"; res.textContent = `✓ ${r.detail || ""}${r.latency_ms ? ` (${r.latency_ms} ms)` : ""}`; }
    else showError(res, r, r.error || r.detail);
  });
  if (s.base_url && !(same && same.checked)) load();
  return div;
}
async function openSettings() {
  SETTINGS = await getJSON("/api/settings");
  const grid = $("#svc-grid");
  grid.innerHTML = "";
  for (const svc of SERVICES) grid.append(svcCard(svc));
  $("#set-thinking").value = SETTINGS.llm.thinking;
  $("#set-thinking-control").value = SETTINGS.llm.thinking_control;
  $("#set-tools").value = SETTINGS.llm.tools;
  $("#set-judge").checked = SETTINGS.answer.judge;
  $("#set-regenerate").checked = SETTINGS.answer.regenerate;
  $("#set-failed").value = SETTINGS.answer.failed_claims;
  $("#set-vision-enabled").checked = SETTINGS.vision.enabled;
  $("#set-tls-system").checked = SETTINGS.tls.system_store;
  $("#set-tls-proxy").checked = SETTINGS.tls.use_system_proxy;
  $("#set-tls-ca").value = SETTINGS.tls.ca_bundle || "";
  $("#settings-status").textContent = "";
  $$("#dlg-settings [data-i18n]").forEach((x) => { x.textContent = t(x.dataset.i18n); });
  $("#dlg-settings").showModal();
}
async function saveSettings(e) {
  e.preventDefault();
  const patch = { answer: { judge: $("#set-judge").checked, regenerate: $("#set-regenerate").checked, failed_claims: $("#set-failed").value } };
  for (const card of $$(".svc")) {
    const svc = card.dataset.svc;
    const same = $(".same", card);
    if (same && same.checked) { patch[svc] = { base_url: "", model: "", api_key: "" }; continue; }
    patch[svc] = { base_url: $(".url", card).value.trim(), api_key: $(".key", card).value, model: $(".model", card).value,
      verify_ssl: !$(".insecure", card).checked };
    const en = $(".enabled", card);
    if (en) patch[svc].enabled = en.checked;
  }
  Object.assign(patch.llm, { thinking: $("#set-thinking").value, thinking_control: $("#set-thinking-control").value, tools: $("#set-tools").value });
  patch.vision = Object.assign(patch.vision || {}, { enabled: $("#set-vision-enabled").checked });
  patch.tls = tlsPatch();
  try {
    const r = await postJSON("/api/settings", patch, "PUT");
    $("#settings-status").textContent = r.error ? `${t("errorPrefix")}: ${r.error}` : t("savedSettings");
    await loadInfo();
    await loadDocs();
    if (!r.error) setTimeout(() => $("#dlg-settings").close(), 500);
  } catch (err) { $("#settings-status").textContent = `${t("errorPrefix")}: ${err.message}`; }
}

function tlsPatch() {
  return { system_store: $("#set-tls-system").checked, use_system_proxy: $("#set-tls-proxy").checked,
    ca_bundle: $("#set-tls-ca").value.trim() };
}
async function applyTls() {
  try {
    const r = await postJSON("/api/settings", { tls: tlsPatch() }, "PUT");
    SETTINGS.tls = r.settings.tls;
    $("#settings-status").textContent = r.error ? `${t("errorPrefix")}: ${r.error}` : t("tlsApplied");
  } catch (e) { $("#settings-status").textContent = `${t("errorPrefix")}: ${e.message}`; }
}
async function pickCaFiles() {
  let paths = [];
  if (desk()) paths = (await desk().pick_files()) || [];
  else { const p = prompt(t("tlsCa"), ""); if (p) paths = [p]; }
  if (!paths.length) return;
  const cur = $("#set-tls-ca").value.split(/[;\n]/).map((x) => x.trim()).filter(Boolean);
  $("#set-tls-ca").value = [...new Set([...cur, ...paths])].join(";");
  await applyTls();
}

// ------------------------------------------------------------------ library dialog
async function pickFolder(input) {
  if (desk()) { const p = await desk().pick_folder(); if (p) input.value = p; }
  else { const p = prompt(t("pickFolderPrompt"), input.value); if (p) input.value = p; }
}
function openLibrary() {
  const i = state.info;
  $("#lib-path").textContent = i ? i.library : "";
  $("#lib-ro").hidden = !(i && i.read_only);
  $("#lib-new-path").value = i ? i.library : "";
  $("#lib-readonly").checked = !!(i && i.read_only);
  $("#lib-status").textContent = "";
  $$("#dlg-library [data-i18n]").forEach((x) => { x.textContent = t(x.dataset.i18n); });
  $("#dlg-library").showModal();
}

// ------------------------------------------------------------------ wiring
function bind() {
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); const q = $("#question").value.trim(); if (q) { $("#question").value = ""; ask(q); } });
  $("#question").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#messages").addEventListener("click", (e) => {
    const c = e.target.closest("button.cite");
    if (!c) return;
    const msg = c.closest(".msg");
    const s = (msg && msg._sources || []).find((x) => x.n === +c.dataset.n);
    if (s) showSource(s);
  });
  $$(".tab").forEach((b) => b.addEventListener("click", () => openTab(b.dataset.tab)));
  $("#btn-new-chat").addEventListener("click", () => { state.history = []; $("#messages").innerHTML = `<div class="empty"><p>${t("newChatStarted")}</p></div>`; });
  $$(".lang").forEach((b) => b.addEventListener("click", () => {
    LANG = b.dataset.lang;
    safeSet("techrag.lang", LANG);
    applyI18n();
    renderNotes();
    renderCollections();
    postJSON("/api/settings", { ui: { language: LANG } }, "PUT").catch(() => {});
  }));
  $("#btn-settings").addEventListener("click", () => openSettings().catch((e) => alert(e.message)));
  $("#btn-save-settings").addEventListener("click", saveSettings);
  ["#set-tls-system", "#set-tls-proxy", "#set-tls-ca"].forEach((id) => $(id).addEventListener("change", applyTls));
  $("#btn-pick-ca").addEventListener("click", pickCaFiles);
  $("#btn-library").addEventListener("click", openLibrary);
  $("#btn-pick-lib").addEventListener("click", () => pickFolder($("#lib-new-path")));
  $("#btn-pick-publish").addEventListener("click", () => pickFolder($("#lib-publish-path")));
  $("#btn-open-lib").addEventListener("click", async () => {
    try {
      const r = await postJSON("/api/library/open", { path: $("#lib-new-path").value, read_only: $("#lib-readonly").checked });
      $("#lib-status").textContent = r.error ? `${t("errorPrefix")}: ${r.error}` : t("opened");
      await loadInfo();
      await loadDocs();
      $("#lib-path").textContent = state.info.library;
    } catch (e) { $("#lib-status").textContent = `${t("errorPrefix")}: ${e.message}`; }
  });
  $("#btn-publish").addEventListener("click", async () => {
    try { const r = await postJSON("/api/library/publish", { dest: $("#lib-publish-path").value }); $("#lib-status").textContent = t("published", r.path); }
    catch (e) { $("#lib-status").textContent = `${t("errorPrefix")}: ${e.message}`; }
  });
  $("#btn-reindex-all").addEventListener("click", () => { $("#dlg-library").close(); reindex($("#lib-rebuild").checked); });
  $("#btn-add-files").addEventListener("click", addFiles);
  $("#add-file-input").addEventListener("change", (e) => uploadFiles([...e.target.files]));
  $("#btn-reindex").addEventListener("click", () => reindex(false));
  $("#btn-export-notes").addEventListener("click", exportNotes);
  $("#btn-clear-notes").addEventListener("click", () => { if (confirm(t("confirmClear"))) { state.notes = []; saveNotes(); } });
}

(async function init() {
  LANG = safeGet("techrag.lang") || LANG;
  bind();
  applyI18n();
  renderNotes();
  try {
    await loadInfo();
    applyI18n();
    await loadDocs();
  } catch (e) {
    const b = $("#banner");
    b.hidden = false;
    b.textContent = `${t("errorPrefix")}: ${e.message}`;
  }
})();
