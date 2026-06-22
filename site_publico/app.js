const DATA_URL = "../site_data/votos.jsonl";

const state = {
  mode: "static",
  query: "",
  instancia: "todas",
  searchMode: "phrase",
  month: "",
  year: "",
  offset: 0,
  limit: 50,
  total: 0,
  items: [],
  records: [],
  byId: new Map(),
  selected: null,
  previewMode: "file",
  loadingData: false,
};

const els = {
  stats: document.querySelector("#stats"),
  form: document.querySelector("#search-form"),
  input: document.querySelector("#search-input"),
  instance: document.querySelector("#instance-filter"),
  searchMode: document.querySelector("#search-mode"),
  month: document.querySelector("#month-filter"),
  year: document.querySelector("#year-filter"),
  resultCount: document.querySelector("#result-count"),
  results: document.querySelector("#results"),
  paginationBar: document.querySelector("#pagination-bar"),
  pagePrev: document.querySelector("#page-prev"),
  pageNext: document.querySelector("#page-next"),
  pageInfo: document.querySelector("#page-info"),
  empty: document.querySelector("#empty-state"),
  detailPane: document.querySelector(".detail-pane"),
  detail: document.querySelector("#detail"),
  detailTitle: document.querySelector("#detail-title"),
  detailSubtitle: document.querySelector("#detail-subtitle"),
  detailInstance: document.querySelector("#detail-instance"),
  openFile: document.querySelector("#open-file"),
  autos: document.querySelector("#autos"),
  preview: document.querySelector("#preview"),
  previewLabel: document.querySelector("#preview-label"),
  showText: document.querySelector("#show-text"),
  showFile: document.querySelector("#show-file"),
};

function escapeHtml(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function snippetHtml(value = "") {
  return escapeHtml(value)
    .replaceAll("&lt;mark&gt;", "<mark>")
    .replaceAll("&lt;/mark&gt;", "</mark>");
}

function normalize(value = "") {
  return String(value)
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function tokenize(query) {
  return normalize(query).match(/[\p{L}\p{N}]+/gu) || [];
}

function hiddenDatePhrase() {
  if (state.month && state.year) return normalize(`${state.month} de ${state.year}`).trim();
  if (state.year) return normalize(`de ${state.year}`).trim();
  return "";
}

function driveViewUrl(fileId) {
  return `https://drive.google.com/file/d/${encodeURIComponent(fileId)}/view`;
}

function drivePreviewUrl(fileId) {
  return `https://drive.google.com/file/d/${encodeURIComponent(fileId)}/preview`;
}

function localFileUrl(fileId) {
  return `/api/file/${encodeURIComponent(fileId)}`;
}

function decorate(record) {
  const fileId = record.file_id;
  const path = record.caminho_bruto || "";
  const ext = path.split(".").pop()?.toLowerCase() || "";
  const publicFileId = record.drive_file_id_publico || record.public_drive_file_id || "";
  record.ext = ext;
  record.file_url = record.file_url || localFileUrl(fileId);
  if (publicFileId) {
    record.drive_view_url = driveViewUrl(publicFileId);
    record.drive_preview_url = drivePreviewUrl(publicFileId);
  }
  record.tem_arquivo_publico = Boolean(publicFileId);
  return record;
}

function searchBlob(record) {
  if (record._search_blob) return record._search_blob;
  const autos = (record.autos || [])
    .map((auto) =>
      [
        auto.numero,
        auto.autuado,
        auto.infracao,
        auto.dispositivo_legal_transgredido,
        auto.local_constatacao,
        auto.lei,
      ]
        .filter(Boolean)
        .join(" ")
    )
    .join(" ");
  record._search_blob = normalize(
    [
      record.nome_arquivo,
      record.decisao_instancia,
      record.protocolo,
      record.assunto,
      autos,
      record.texto,
    ]
      .filter(Boolean)
      .join("\n")
  );
  return record._search_blob;
}

function dateFilterMatches(record) {
  const datePhrase = hiddenDatePhrase();
  if (!datePhrase) return true;
  return searchBlob(record).includes(datePhrase);
}

function scoreRecord(record, terms, phrase) {
  if (!dateFilterMatches(record)) return 0;
  if (!terms.length) return 1;
  const title = normalize(record.nome_arquivo || "");
  const subject = normalize(record.assunto || "");
  const autos = normalize((record.autos || []).map((auto) => Object.values(auto).join(" ")).join(" "));
  const blob = searchBlob(record);
  let score = 0;
  if (state.searchMode === "phrase") {
    if (!blob.includes(phrase)) return 0;
    if (title.includes(phrase)) score += 30;
    if (subject.includes(phrase)) score += 22;
    if (autos.includes(phrase)) score += 16;
    score += 8 + terms.length;
    for (const term of terms) {
      if (title.includes(term)) score += 2;
      if (subject.includes(term)) score += 2;
      if (autos.includes(term)) score += 1;
    }
    return score;
  }
  if (!terms.every((term) => blob.includes(term))) return 0;
  for (const term of terms) {
    if (title.includes(term)) score += 10;
    if (subject.includes(term)) score += 7;
    if (autos.includes(term)) score += 5;
    score += 1;
  }
  return score;
}

function makeSnippet(record, terms, phrase) {
  const source = record.assunto || record.texto || record.nome_arquivo || "";
  if (!terms.length) return escapeHtml(source.slice(0, 360));
  const normalized = normalize(source);
  let index = state.searchMode === "phrase" ? normalized.indexOf(phrase) : -1;
  if (index === -1) {
    for (const term of terms) {
      index = normalized.indexOf(term);
      if (index !== -1) break;
    }
  }
  const start = Math.max(index - 130, 0);
  const raw = source.slice(start, start + 420);
  const prefix = start > 0 ? "..." : "";
  return prefix + highlight(raw, terms);
}

function fillYearFilter(records) {
  const currentYear = new Date().getFullYear();
  const years = [];
  for (let year = currentYear; year >= 2022; year -= 1) {
    years.push(String(year));
  }
  els.year.innerHTML = '<option value="">Ano</option>' + years.map((year) => `<option value="${year}">${year}</option>`).join("");
}

function highlight(value, terms) {
  const source = String(value || "");
  const needles = [...new Set(terms.map(normalize).filter(Boolean))].sort(
    (a, b) => b.length - a.length
  );
  if (!source || !needles.length) return escapeHtml(source);

  let normalized = "";
  const starts = [];
  const ends = [];
  let offset = 0;
  for (const character of source) {
    const start = offset;
    offset += character.length;
    const folded = normalize(character);
    normalized += folded;
    for (let index = 0; index < folded.length; index += 1) {
      starts.push(start);
      ends.push(offset);
    }
  }

  const ranges = [];
  for (const needle of needles) {
    let from = 0;
    while (from < normalized.length) {
      const index = normalized.indexOf(needle, from);
      if (index === -1) break;
      ranges.push([starts[index], ends[index + needle.length - 1]]);
      from = index + needle.length;
    }
  }

  if (!ranges.length) return escapeHtml(source);
  ranges.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged = [];
  for (const range of ranges) {
    const previous = merged.at(-1);
    if (previous && range[0] <= previous[1]) {
      previous[1] = Math.max(previous[1], range[1]);
    } else {
      merged.push([...range]);
    }
  }

  let html = "";
  let cursor = 0;
  for (const [start, end] of merged) {
    html += escapeHtml(source.slice(cursor, start));
    html += `<mark>${escapeHtml(source.slice(start, end))}</mark>`;
    cursor = end;
  }
  return html + escapeHtml(source.slice(cursor));
}

function scrollToFirstHighlight() {
  window.requestAnimationFrame(() => {
    const firstMatch = els.preview.querySelector("mark");
    if (firstMatch) firstMatch.scrollIntoView({ block: "center", inline: "nearest" });
  });
}

function debounce(fn, wait = 250) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function apiAvailable() {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 600);
    const response = await fetch("/api/stats", { signal: controller.signal });
    clearTimeout(timer);
    return response.ok;
  } catch {
    return false;
  }
}

async function loadStaticData() {
  if (state.records.length || state.loadingData) return;
  state.loadingData = true;
  els.stats.textContent = "Carregando índice";
  const response = await fetch(DATA_URL);
  if (!response.ok) throw new Error(`Não foi possível carregar ${DATA_URL}`);
  const text = await response.text();
  const records = [];
  for (const line of text.split(/\n+/)) {
    if (!line.trim()) continue;
    const record = decorate(JSON.parse(line));
    records.push(record);
    state.byId.set(record.file_id, record);
  }
  state.records = records;
  state.loadingData = false;
  fillYearFilter(records);
  const autos = records.reduce((sum, record) => sum + (record.autos?.length || 0), 0);
  els.stats.textContent = `${records.length.toLocaleString("pt-BR")} votos · ${autos.toLocaleString("pt-BR")} autos`;
}

async function init() {
  if (await apiAvailable()) {
    state.mode = "api";
    const stats = await fetchJson("/api/stats");
    els.stats.textContent = `${stats.votos.toLocaleString("pt-BR")} votos · ${stats.autos.toLocaleString("pt-BR")} autos`;
  } else {
    state.mode = "static";
    await loadStaticData();
  }
  await runSearch();
}

function searchUrl(fetchOffset) {
  const params = new URLSearchParams({
    q: state.query,
    instancia: state.instancia,
    modo: state.searchMode,
    mes: state.month,
    ano: state.year,
    limit: String(state.limit),
    offset: String(fetchOffset),
  });
  return `/api/search?${params.toString()}`;
}

async function runSearch({ append = false, keepOffset = false } = {}) {
  if (!append && !keepOffset) {
    state.offset = 0;
  }
  if (!append) {
    state.items = [];
    els.results.innerHTML = '<div class="loading">Buscando...</div>';
  }

  let fetchOffset = state.offset;
  if (append) {
    fetchOffset = state.offset + state.items.length;
  }

  if (state.mode === "api") {
    const data = await fetchJson(searchUrl(fetchOffset));
    state.total = data.total;
    state.items = append ? [...state.items, ...data.items.map(decorate)] : data.items.map(decorate);
  } else {
    await loadStaticData();
    const terms = tokenize(state.query);
    const phrase = normalize(state.query).trim();
    const filtered = state.records
      .filter((record) => state.instancia === "todas" || record.instancia === state.instancia)
      .map((record) => ({ record, score: scoreRecord(record, terms, phrase) }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score || a.record.nome_arquivo.localeCompare(b.record.nome_arquivo, "pt-BR"));
      
    state.total = filtered.length;
    
    const pageItems = filtered.slice(fetchOffset, fetchOffset + state.limit).map((entry) => ({
      ...entry.record,
      trecho: makeSnippet(entry.record, terms, phrase),
    }));

    state.items = append ? [...state.items, ...pageItems] : pageItems;
  }

  renderResults();
}

function renderResults() {
  const shown = state.items.length;
  const endOfShown = state.offset + shown;
  els.resultCount.textContent = `${endOfShown.toLocaleString("pt-BR")} de ${state.total.toLocaleString("pt-BR")} resultados`;

  if (state.total > state.limit) {
    els.paginationBar.hidden = false;
    const startPage = Math.floor(state.offset / state.limit) + 1;
    const endPage = Math.floor((state.offset + shown - 1) / state.limit) + 1;
    const totalPages = Math.ceil(state.total / state.limit);
    
    if (startPage === endPage) {
      els.pageInfo.textContent = `Página ${startPage} de ${totalPages}`;
    } else {
      els.pageInfo.textContent = `Páginas ${startPage}-${endPage} de ${totalPages}`;
    }

    els.pagePrev.disabled = state.offset === 0;
    els.pageNext.disabled = endOfShown >= state.total;
  } else {
    if (els.paginationBar) els.paginationBar.hidden = true;
  }

  if (!shown) {
    els.results.innerHTML = '<div class="empty-list">Nenhum resultado</div>';
    return;
  }

  els.results.innerHTML = state.items
    .map((item) => {
      const active = state.selected?.file_id === item.file_id ? " active" : "";
      const autos = (item.autos || []).slice(0, 4).map((auto) => `<span class="chip">${escapeHtml(auto.numero)}</span>`).join("");
      return `
        <button class="result${active}" type="button" data-file-id="${escapeHtml(item.file_id)}">
          <span class="result-title">${escapeHtml(item.nome_arquivo)}</span>
          <span class="result-meta">${escapeHtml(item.decisao_instancia)} · ${escapeHtml(item.protocolo || "sem protocolo")}</span>
          <span class="result-snippet">${state.mode === "api" ? snippetHtml(item.trecho || "") : item.trecho || ""}</span>
          <span class="result-autos">${autos || '<span class="chip">sem auto</span>'}</span>
        </button>
      `;
    })
    .join("");
}

async function selectVoto(fileId) {
  if (state.mode === "api") {
    state.selected = decorate(await fetchJson(`/api/voto/${encodeURIComponent(fileId)}`));
  } else {
    state.selected = state.byId.get(fileId);
  }
  state.previewMode = chooseInitialPreviewMode(state.selected);
  renderResults();
  renderDetail();
}

function renderDetail() {
  const item = state.selected;
  if (!item) {
    els.empty.hidden = false;
    els.empty.classList.remove("is-hidden");
    els.detail.hidden = true;
    return;
  }

  els.empty.classList.add("is-hidden");
  window.setTimeout(() => {
    if (state.selected) els.empty.hidden = true;
  }, 220);
  els.detail.hidden = false;
  els.detailPane.scrollTop = 0;
  els.detailTitle.textContent = item.nome_arquivo;
  els.detailSubtitle.textContent = item.assunto || item.protocolo || "";
  els.detailInstance.textContent = item.decisao_instancia;
  if (item.tem_arquivo_publico) {
    els.openFile.href = item.drive_view_url;
    els.openFile.removeAttribute("aria-disabled");
    els.openFile.textContent = "Abrir inteiro";
  } else {
    els.openFile.removeAttribute("href");
    els.openFile.setAttribute("aria-disabled", "true");
    els.openFile.textContent = "Sem arquivo público";
  }
  els.autos.innerHTML = renderAutos(item.autos || []);
  renderPreview();
}

function renderAutos(autos = []) {
  if (!autos.length) return '<div class="auto-row"><strong>Sem autos no assunto</strong></div>';
  return autos
    .map(
      (auto) => {
        let numeroHtml = `<strong>${escapeHtml(auto.numero)}</strong>`;
        if (auto.numero) {
          const clean = String(auto.numero).replace(/[^a-z0-9]/gi, "");
          const match = clean.match(/^(\d+)([a-z]*)$/i);
          if (match) {
            const idn = encodeURIComponent(match[1]);
            const tip = encodeURIComponent((match[2] || "").toUpperCase());
            const sifUrl = `https://sif-piloto.pbh.gov.br/MostraAuto.php?s_Tip_Auto=&s_Idn_Doct_Lavr=${idn}&s_Dat_Lavr=&s_Dat_Lavr2=&s_Dat_Fina=&s_Nom_Raza_Soci=&s_Nom_Fant=&s_Num_Cpf_CGC=&Idn_Equp=&Fiscal=&s_Idn_Cmpo_Lei=&s_Tip_Logr=&s_Nom_Logr=&s_Num_Imov_Logr=&s_Nom_Bair=&s_Num_CEP=&s_Tip_Loca=&s_Nom_Loca=&s_Num_Imov_Loca=&s_Nom_Bair_Loca=&s_Num_CEP_Loca=&s_Tip_Cienc=&s_Sit_Regt=A&Idn_Doct_Lavr=${idn}&Tip_Auto=${tip}`;
            numeroHtml = `<a href="${sifUrl}" target="_blank" rel="noreferrer" class="auto-link"><strong>${escapeHtml(auto.numero)}</strong></a>`;
          }
        }

        return `
      <section class="auto-row">
        <div>
          ${numeroHtml}
          <span class="badge">${auto.pdf_encontrado ? "SIF" : "sem PDF"}</span>
        </div>
        <div class="auto-fields">
          <div><span>Autuado</span> ${escapeHtml(auto.autuado || "—")}</div>
          <div><span>Infração</span> ${escapeHtml(auto.infracao || "—")}</div>
          <div><span>Dispositivo</span> ${escapeHtml(auto.dispositivo_legal_transgredido || "—")}</div>
          <div><span>Local</span> ${escapeHtml(auto.local_constatacao || "—")}</div>
        </div>
      </section>
    `;
      }
    )
    .join("");
}

function autosLabel(autos = []) {
  if (!autos.length) return "sem auto";
  return autos.map((auto) => auto.numero).join(", ");
}

function prefersTextPreview(item) {
  return ["doc", "docx", "txt", "rtf"].includes((item.ext || "").toLowerCase());
}

function previewKindLabel(item) {
  if (prefersTextPreview(item)) return "prévia textual";
  return "prévia do arquivo";
}

function chooseInitialPreviewMode(item) {
  if (prefersTextPreview(item)) return "text";
  return "file";
}

function renderPreview() {
  const item = state.selected;
  if (!item) return;

  const terms = tokenize(state.query);
  const isFilePreview = state.previewMode === "file";
  els.showFile.classList.toggle("active", isFilePreview);
  els.showText.classList.toggle("active", !isFilePreview);
  els.showText.textContent = terms.length ? "Texto com destaques" : "Texto";
  els.showFile.disabled = !item.tem_arquivo_publico;
  els.previewLabel.textContent = `${(item.ext || "arquivo").toUpperCase()} · ${previewKindLabel(item)} · ${autosLabel(item.autos || [])}`;

  if (isFilePreview && item.tem_arquivo_publico) {
    els.preview.innerHTML = `<iframe title="Prévia do arquivo" src="${escapeHtml(item.drive_preview_url)}"></iframe>`;
    return;
  }

  const text = item.texto_preview || item.texto || "Texto não disponível.";
  let notice = "";
  if (!item.tem_arquivo_publico) {
    notice = "Arquivo ainda não encontrado na pasta pública do Drive. Exibindo prévia textual extraída do índice.\n\n";
  } else if (prefersTextPreview(item)) {
    notice = "Prévia textual extraída do Word/texto. Use “Abrir inteiro” para ver o arquivo original no Drive.\n\n";
  }
  if (terms.length) {
    notice += "Termos da busca destacados em amarelo.\n\n";
  }
  els.preview.innerHTML = `<div class="text-preview">${escapeHtml(notice)}${highlight(text, terms)}</div>`;
  scrollToFirstHighlight();
}

els.form.addEventListener("submit", (event) => {
  event.preventDefault();
  state.query = els.input.value.trim();
  state.instancia = els.instance.value;
  state.searchMode = els.searchMode.value;
  state.month = els.month.value;
  state.year = els.year.value;
  runSearch();
});

els.input.addEventListener(
  "input",
  debounce(() => {
    state.query = els.input.value.trim();
    runSearch();
  }, 300)
);

els.instance.addEventListener("change", () => {
  state.instancia = els.instance.value;
  runSearch();
});

els.searchMode.addEventListener("change", () => {
  state.searchMode = els.searchMode.value;
  runSearch();
});

els.month.addEventListener("change", () => {
  state.month = els.month.value;
  runSearch();
});

els.year.addEventListener("change", () => {
  state.year = els.year.value;
  runSearch();
});

els.results.addEventListener("click", (event) => {
  const button = event.target.closest("[data-file-id]");
  if (!button) return;
  selectVoto(button.dataset.fileId);
});

els.pageNext.addEventListener("click", () => {
  state.offset = state.offset + state.items.length;
  runSearch({ append: false, keepOffset: true });
});

els.pagePrev.addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  runSearch({ append: false, keepOffset: true });
});

els.showText.addEventListener("click", () => {
  state.previewMode = "text";
  renderPreview();
});

els.showFile.addEventListener("click", () => {
  state.previewMode = "file";
  renderPreview();
});

init().catch((error) => {
  els.stats.textContent = "Falha ao carregar";
  els.results.innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`;
});
