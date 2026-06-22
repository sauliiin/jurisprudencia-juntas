const DATA_URL = "../../site_data/pareceres.jsonl";

const state = {
  query: "",
  categoria: "todas",
  searchMode: "phrase",
  offset: 0,
  limit: 50,
  total: 0,
  items: [],
  records: [],
  byId: new Map(),
  selected: null,
  previewMode: "text",
  loadingData: false,
};

const els = {
  stats: document.querySelector("#stats"),
  form: document.querySelector("#search-form"),
  input: document.querySelector("#search-input"),
  category: document.querySelector("#category-filter"),
  searchMode: document.querySelector("#search-mode"),
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
  detailCategory: document.querySelector("#detail-category"),
  openFile: document.querySelector("#open-file"),
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

function normalize(value = "") {
  return String(value)
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase();
}

function tokenize(query) {
  return normalize(query).match(/[\p{L}\p{N}]+/gu) || [];
}

function decorate(record) {
  record.tem_arquivo_publico = Boolean(record.drive_preview_url);
  return record;
}

function searchBlob(record) {
  if (record._search_blob) return record._search_blob;
  record._search_blob = normalize(
    [record.nome_arquivo, record.categoria, record.pasta, record.texto]
      .filter(Boolean)
      .join("\n")
  );
  return record._search_blob;
}

function scoreRecord(record, terms, phrase) {
  if (!terms.length) return 1;
  const title = normalize(record.nome_arquivo || "");
  const pasta = normalize(record.pasta || "");
  const blob = searchBlob(record);
  let score = 0;
  if (state.searchMode === "phrase") {
    if (!blob.includes(phrase)) return 0;
    if (title.includes(phrase)) score += 30;
    if (pasta.includes(phrase)) score += 16;
    score += 8 + terms.length;
    for (const term of terms) {
      if (title.includes(term)) score += 2;
      if (pasta.includes(term)) score += 1;
    }
    return score;
  }
  if (!terms.every((term) => blob.includes(term))) return 0;
  for (const term of terms) {
    if (title.includes(term)) score += 10;
    if (pasta.includes(term)) score += 4;
    score += 1;
  }
  return score;
}

function makeSnippet(record, terms, phrase) {
  const source = record.texto || record.pasta || record.nome_arquivo || "";
  if (!terms.length) return escapeHtml(source.slice(0, 360));
  const normalized = normalize(source);
  let index = state.searchMode === "phrase" ? normalized.indexOf(phrase) : -1;
  if (index === -1) {
    for (const term of terms) {
      index = normalized.indexOf(term);
      if (index !== -1) break;
    }
  }
  if (index === -1) return escapeHtml(source.slice(0, 360));
  const start = Math.max(index - 130, 0);
  const raw = source.slice(start, start + 420);
  const prefix = start > 0 ? "..." : "";
  return prefix + highlight(raw, terms);
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

function fillCategoryFilter(records) {
  const categorias = [...new Set(records.map((record) => record.categoria).filter(Boolean))].sort(
    (a, b) => a.localeCompare(b, "pt-BR")
  );
  els.category.innerHTML =
    '<option value="todas">Todas as pastas</option>' +
    categorias
      .map((categoria) => `<option value="${escapeHtml(categoria)}">${escapeHtml(categoria)}</option>`)
      .join("");
}

function debounce(fn, wait = 250) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
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
  fillCategoryFilter(records);
  const pastas = new Set(records.map((record) => record.pasta).filter(Boolean)).size;
  els.stats.textContent = `${records.length.toLocaleString("pt-BR")} pareceres · ${pastas.toLocaleString("pt-BR")} pastas`;
}

async function init() {
  await loadStaticData();
  await runSearch();
}

async function runSearch({ keepOffset = false } = {}) {
  if (!keepOffset) state.offset = 0;
  state.items = [];
  els.results.innerHTML = '<div class="loading">Buscando...</div>';

  await loadStaticData();
  const terms = tokenize(state.query);
  const phrase = normalize(state.query).trim();
  const filtered = state.records
    .filter((record) => state.categoria === "todas" || record.categoria === state.categoria)
    .map((record) => ({ record, score: scoreRecord(record, terms, phrase) }))
    .filter((entry) => entry.score > 0)
    .sort(
      (a, b) =>
        b.score - a.score ||
        a.record.nome_arquivo.localeCompare(b.record.nome_arquivo, "pt-BR")
    );

  state.total = filtered.length;
  state.items = filtered.slice(state.offset, state.offset + state.limit).map((entry) => ({
    ...entry.record,
    trecho: makeSnippet(entry.record, terms, phrase),
  }));

  renderResults();
}

function renderResults() {
  const shown = state.items.length;
  const endOfShown = state.offset + shown;
  els.resultCount.textContent = `${endOfShown.toLocaleString("pt-BR")} de ${state.total.toLocaleString("pt-BR")} resultados`;

  if (state.total > state.limit) {
    els.paginationBar.hidden = false;
    const startPage = Math.floor(state.offset / state.limit) + 1;
    const totalPages = Math.ceil(state.total / state.limit);
    els.pageInfo.textContent = `Página ${startPage} de ${totalPages}`;
    els.pagePrev.disabled = state.offset === 0;
    els.pageNext.disabled = endOfShown >= state.total;
  } else {
    els.paginationBar.hidden = true;
  }

  if (!shown) {
    els.results.innerHTML = '<div class="empty-list">Nenhum resultado</div>';
    return;
  }

  els.results.innerHTML = state.items
    .map((item) => {
      const active = state.selected?.file_id === item.file_id ? " active" : "";
      const pasta = item.pasta && item.pasta !== "Raiz"
        ? `<span class="chip">${escapeHtml(item.pasta)}</span>`
        : "";
      return `
        <button class="result${active}" type="button" data-file-id="${escapeHtml(item.file_id)}">
          <span class="result-title">${escapeHtml(item.nome_arquivo)}</span>
          <span class="result-meta">${escapeHtml(item.categoria || "Raiz")} · ${escapeHtml((item.ext || "").toUpperCase())}${item.ocr ? " · OCR" : ""}</span>
          <span class="result-snippet">${item.trecho || ""}</span>
          <span class="result-autos">${pasta || '<span class="chip">raiz</span>'}</span>
        </button>
      `;
    })
    .join("");
}

function selectParecer(fileId) {
  state.selected = state.byId.get(fileId);
  state.previewMode = "text";
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
  els.detailSubtitle.textContent = item.pasta && item.pasta !== "Raiz" ? item.pasta : "";
  els.detailCategory.textContent = `${item.categoria || "Raiz"} · ${(item.ext || "").toUpperCase()}${item.ocr ? " · OCR" : ""}`;
  if (item.tem_arquivo_publico) {
    els.openFile.href = item.drive_view_url;
    els.openFile.removeAttribute("aria-disabled");
    els.openFile.textContent = "Abrir inteiro";
  } else {
    els.openFile.removeAttribute("href");
    els.openFile.setAttribute("aria-disabled", "true");
    els.openFile.textContent = "Sem arquivo";
  }
  renderPreview();
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
  els.previewLabel.textContent = `${(item.ext || "arquivo").toUpperCase()} · ${
    isFilePreview ? "prévia do arquivo" : "prévia textual"
  }`;

  if (isFilePreview && item.tem_arquivo_publico) {
    els.preview.innerHTML = `<iframe title="Prévia do arquivo" src="${escapeHtml(item.drive_preview_url)}"></iframe>`;
    return;
  }

  const text = item.texto || "Texto não disponível para este documento.";
  let notice = "";
  if (item.ocr) {
    notice = "Texto reconhecido por OCR (documento escaneado); pode conter erros. Use “Arquivo” para ver o original do Drive.\n\n";
  } else if (item.tem_arquivo_publico) {
    notice = "Prévia textual extraída do documento. Use “Arquivo” para ver o original do Drive ou “Abrir inteiro” para abrir em nova aba.\n\n";
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
  state.categoria = els.category.value;
  state.searchMode = els.searchMode.value;
  runSearch();
});

els.input.addEventListener(
  "input",
  debounce(() => {
    state.query = els.input.value.trim();
    runSearch();
  }, 300)
);

els.category.addEventListener("change", () => {
  state.categoria = els.category.value;
  runSearch();
});

els.searchMode.addEventListener("change", () => {
  state.searchMode = els.searchMode.value;
  runSearch();
});

els.results.addEventListener("click", (event) => {
  const button = event.target.closest("[data-file-id]");
  if (!button) return;
  selectParecer(button.dataset.fileId);
});

els.pageNext.addEventListener("click", () => {
  state.offset = state.offset + state.limit;
  runSearch({ keepOffset: true });
});

els.pagePrev.addEventListener("click", () => {
  state.offset = Math.max(0, state.offset - state.limit);
  runSearch({ keepOffset: true });
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
