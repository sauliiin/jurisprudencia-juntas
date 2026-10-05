// Este arquivo também serve o modo Legislação (../legislacao/index.html), que muda
// o acervo e os rótulos pelos atributos data-* do <body>. Sem eles, vale Pareceres.
// Na legislação cada documento traz "dispositivos" (artigos/itens de tabela): eles
// viram os resultados da busca, e data-ia liga a busca por significado e os artigos
// semelhantes (../ia.js).
const ACERVO = {
  dados: document.body.dataset.acervo || "../../site_data/pareceres.jsonl",
  documentos: document.body.dataset.documentos || "pareceres",
  categorias: document.body.dataset.categorias || "pastas",
  previa: document.body.dataset.previa || "file",
  ia: document.body.dataset.ia === "legislacao" && typeof IA !== "undefined",
};

// Busca por significado: só os mais próximos interessam; abaixo disso vira ruído.
const IA_MAX_RESULTADOS = 200;
const IA_SEMELHANTES = 5;

const state = {
  query: "",
  categoria: "todas",
  // O padrão vem do HTML: Significado (IA) na legislação, Expressão nos pareceres.
  searchMode: document.querySelector("#search-mode").value,
  modoEfetivo: "phrase",
  iaRecurso: "",
  offset: 0,
  limit: 50,
  total: 0,
  items: [],
  records: [],
  byId: new Map(),
  selected: null,
  previewMode: ACERVO.previa,
  loadingData: false,
  iaTermos: 0,
  arquivos: 0,
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
  similares: document.querySelector("#similares"),
  iaHint: document.querySelector("#ia-hint"),
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

// Redação revogada continua pesquisável, mas abaixo da vigente.
function scoreRecord(record, terms, phrase) {
  const score = scoreText(record, terms, phrase);
  return record.revogado ? score / 2 : score;
}

function scoreText(record, terms, phrase) {
  if (!terms.length) return 1;
  const title = normalize(record.nome_arquivo || "");
  const pasta = normalize(record.pasta || "");
  const blob = searchBlob(record);
  let score = 0;
  if (state.modoEfetivo === "phrase") {
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
  let index = state.modoEfetivo === "phrase" ? normalized.indexOf(phrase) : -1;
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
  return prefix + highlight(raw, highlightNeedles(terms));
}

// O que marcar no texto: a expressão inteira (Expressão), cada palavra (Palavras) ou,
// na busca por significado, só as palavras de 4+ letras da consulta. Termo a termo,
// "mesas e cadeiras" marcaria cada "e" do texto.
function highlightNeedles(terms) {
  if (!terms.length) return terms;
  if (state.modoEfetivo === "semantic") return terms.filter((term) => term.length >= 4);
  if (state.modoEfetivo === "phrase") return [normalize(state.query).trim()];
  return terms;
}

// A IA só lê palavras: consulta com número ("art. 77", "Lei 8.616") vai direto para
// a busca por expressão, e a sem nenhum termo conhecido pela IA também.
function pedeExpressao(query) {
  return /\d/.test(query);
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

function scrollToFirstHighlight(alternativa = null) {
  window.requestAnimationFrame(() => {
    const alvo = els.preview.querySelector("mark") || (alternativa && els.preview.querySelector(alternativa));
    if (!alvo) return;
    // Texto com rolagem própria (legislação): rola só a caixa, sem esconder o
    // cabeçalho e os artigos semelhantes acima dela.
    const caixa = els.preview.querySelector(".text-preview");
    if (caixa && caixa.scrollHeight > caixa.clientHeight) {
      const distancia = alvo.getBoundingClientRect().top - caixa.getBoundingClientRect().top;
      caixa.scrollTop += distancia - (alvo.tagName === "MARK" ? caixa.clientHeight / 3 : 12);
      return;
    }
    alvo.scrollIntoView({ block: alvo.tagName === "MARK" ? "center" : "start", inline: "nearest" });
  });
}

function fillCategoryFilter(records) {
  const categorias = [...new Set(records.map((record) => record.categoria).filter(Boolean))].sort(
    (a, b) => a.localeCompare(b, "pt-BR")
  );
  els.category.innerHTML =
    `<option value="todas">Todas as ${escapeHtml(ACERVO.categorias)}</option>` +
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
  const response = await fetch(ACERVO.dados);
  if (!response.ok) throw new Error(`Não foi possível carregar ${ACERVO.dados}`);
  const text = await response.text();
  const records = [];
  for (const line of text.split(/\n+/)) {
    if (!line.trim()) continue;
    const doc = decorate(JSON.parse(line));
    state.arquivos += 1;
    for (const record of doc.dispositivos ? dispositivosDe(doc) : [doc]) {
      record.ordem = records.length;
      records.push(record);
      state.byId.set(record.file_id, record);
    }
  }
  state.records = records;
  state.loadingData = false;
  fillCategoryFilter(records);
  const grupos = new Set(records.map((record) => record.categoria).filter(Boolean)).size;
  const total = records.length.toLocaleString("pt-BR");
  const porDispositivo = records.length !== state.arquivos;
  els.stats.textContent = porDispositivo
    ? `${total} artigos e itens · ${state.arquivos.toLocaleString("pt-BR")} ${ACERVO.documentos} · ${grupos.toLocaleString("pt-BR")} ${ACERVO.categorias}`
    : `${total} ${ACERVO.documentos} · ${grupos.toLocaleString("pt-BR")} ${ACERVO.categorias}`;
}

// Cada artigo (ou item de tabela) vira um resultado; o documento inteiro fica em .doc
// para a prévia mostrar o artigo no meio da norma.
function dispositivosDe(doc) {
  return doc.dispositivos.map((d) => ({
    file_id: `${doc.file_id}#${d.id}`,
    nome_arquivo: d.tipo === "documento" ? doc.nome_arquivo : `${doc.categoria}, ${d.rotulo}`,
    arquivo: doc.nome_arquivo,
    categoria: doc.categoria,
    pasta: d.contexto,
    ext: doc.ext,
    texto: doc.texto.slice(d.inicio, d.fim),
    revogado: d.revogado,
    doc,
    inicio: d.inicio,
    fim: d.fim,
    drive_view_url: doc.drive_view_url,
    drive_preview_url: doc.drive_preview_url,
    tem_arquivo_publico: doc.tem_arquivo_publico,
  }));
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
  let semantic = ACERVO.ia && state.searchMode === "semantic" && state.query;
  let semanticFiltered = null;
  state.iaRecurso = "";
  if (semantic && pedeExpressao(state.query)) {
    semantic = false;
    state.iaRecurso = "numero";
  }
  if (semantic) {
    semanticFiltered = await semanticEntries();
    if (!state.iaTermos) {
      semantic = false;
      state.iaRecurso = "desconhecido";
    }
  }
  state.modoEfetivo = semantic ? "semantic" : state.searchMode === "semantic" ? "phrase" : state.searchMode;
  const filtered = semantic
    ? semanticFiltered
    : state.records
        .filter((record) => state.categoria === "todas" || record.categoria === state.categoria)
        .map((record) => ({ record, score: scoreRecord(record, terms, phrase) }))
        .filter((entry) => entry.score > 0)
        .sort(
          (a, b) =>
            b.score - a.score ||
            (a.record.ordem ?? 0) - (b.record.ordem ?? 0) ||
            a.record.nome_arquivo.localeCompare(b.record.nome_arquivo, "pt-BR")
        );

  state.total = filtered.length;
  state.items = filtered.slice(state.offset, state.offset + state.limit).map((entry) => ({
    ...entry.record,
    ia_similaridade: semantic ? entry.score : null,
    trecho: makeSnippet(entry.record, semantic ? [] : terms, phrase),
  }));

  renderResults();
}

// Ordena os artigos vigentes pela proximidade de significado com a consulta (modelo em ../ia.js).
async function semanticEntries() {
  const { ids, similaridades, termosConhecidos } = await IA.buscarLegislacao(state.query);
  state.iaTermos = termosConhecidos;
  const entries = [];
  for (let i = 0; i < ids.length; i += 1) {
    const record = state.byId.get(ids[i]);
    if (!record || similaridades[i] <= 0) continue;
    if (state.categoria !== "todas" && record.categoria !== state.categoria) continue;
    entries.push({ record, score: similaridades[i] });
  }
  entries.sort((a, b) => b.score - a.score);
  return entries.slice(0, IA_MAX_RESULTADOS);
}

function renderIaHint() {
  if (!els.iaHint) return;
  const ativo = ACERVO.ia && state.searchMode === "semantic" && state.query;
  els.iaHint.hidden = !ativo;
  if (!ativo) return;
  if (state.iaRecurso) {
    els.iaHint.textContent =
      state.iaRecurso === "numero"
        ? "Consulta com número: resultados da busca por expressão exata."
        : "A IA não conhece os termos da consulta: resultados da busca por expressão exata.";
    return;
  }
  els.iaHint.innerHTML =
    'Artigos vigentes ordenados por proximidade de significado. <a href="../ia.html" target="_blank">Como funciona</a>';
}

function renderResults() {
  renderIaHint();
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
      // Na legislação a categoria é a norma; o chip "raiz" só repetiria a pasta única.
      const semPasta = item.categoria === "Raiz" ? '<span class="chip">raiz</span>' : "";
      const extras = [
        item.ocr ? "OCR" : "",
        item.revogado ? "redação revogada" : "",
        item.ia_similaridade ? `similaridade ${Math.round(item.ia_similaridade * 100)}%` : "",
      ].filter(Boolean);
      const origem = item.arquivo || `${item.categoria || "Raiz"} · ${(item.ext || "").toUpperCase()}`;
      return `
        <button class="result${active}" type="button" data-file-id="${escapeHtml(item.file_id)}">
          <span class="result-title">${escapeHtml(item.nome_arquivo)}</span>
          <span class="result-meta">${escapeHtml([origem, ...extras].join(" · "))}</span>
          <span class="result-snippet">${item.trecho || ""}</span>
          <span class="result-autos">${pasta || semPasta}</span>
        </button>
      `;
    })
    .join("");
}

function selectParecer(fileId) {
  state.selected = state.byId.get(fileId);
  state.previewMode = ACERVO.previa;
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
  els.detailCategory.textContent = [
    item.categoria || "Raiz",
    (item.ext || "").toUpperCase(),
    item.ocr ? "OCR" : "",
    item.revogado ? "REDAÇÃO REVOGADA" : "",
  ]
    .filter(Boolean)
    .join(" · ");
  if (item.tem_arquivo_publico) {
    els.openFile.href = item.drive_view_url;
    els.openFile.removeAttribute("aria-disabled");
    els.openFile.textContent = "Abrir inteiro";
  } else {
    els.openFile.removeAttribute("href");
    els.openFile.setAttribute("aria-disabled", "true");
    els.openFile.textContent = "Sem arquivo";
  }
  renderSimilares(item);
  renderPreview();
}

// Artigos semelhantes: vizinhos no espaço do modelo, que junta o texto do artigo às
// decisões cujos autos o citam. Sem os arquivos da IA o bloco não aparece.
async function renderSimilares(item) {
  if (!els.similares) return;
  els.similares.hidden = true;
  els.similares.innerHTML = "";
  if (!ACERVO.ia) return;
  let ids, citacoes;
  try {
    [ids, citacoes] = await Promise.all([
      IA.artigosSemelhantes(item.file_id, IA_SEMELHANTES),
      IA.citacoesDoArtigo(item.file_id),
    ]);
  } catch {
    return;
  }
  if (state.selected?.file_id !== item.file_id) return;
  const vizinhos = ids.map((id) => state.byId.get(id)).filter(Boolean);
  if (!vizinhos.length) return;
  const citado = citacoes
    ? `citado nos autos de ${citacoes.toLocaleString("pt-BR")} ${citacoes === 1 ? "decisão" : "decisões"} do acervo`
    : "";
  els.similares.innerHTML = `
    <div class="similares-head">
      <strong>Artigos semelhantes</strong>
      <span class="badge">IA</span>
      <span class="similares-resumo">${escapeHtml(citado)}</span>
      <a href="../ia.html" target="_blank">Como funciona</a>
    </div>
    ${vizinhos
      .map(
        (vizinho) => `
      <button class="similar" type="button" data-file-id="${escapeHtml(vizinho.file_id)}">
        <span class="similar-title">${escapeHtml(vizinho.nome_arquivo)}</span>
        <span class="similar-meta">${escapeHtml(vizinho.pasta || vizinho.arquivo || "")}</span>
        <span class="similar-infracao">${escapeHtml(vizinho.texto.slice(0, 160))}</span>
      </button>`
      )
      .join("")}
  `;
  els.similares.hidden = false;
}

function renderPreview() {
  const item = state.selected;
  if (!item) return;

  const terms = tokenize(state.query);
  const needles = highlightNeedles(terms);
  const isFilePreview = state.previewMode === "file";
  els.showFile.classList.toggle("active", isFilePreview);
  els.showText.classList.toggle("active", !isFilePreview);
  els.showText.textContent = needles.length ? "Texto com destaques" : "Texto";
  els.showFile.disabled = !item.tem_arquivo_publico;
  els.previewLabel.textContent = `${(item.ext || "arquivo").toUpperCase()} · ${
    isFilePreview ? "prévia do arquivo" : "prévia textual"
  }`;

  if (isFilePreview && item.tem_arquivo_publico) {
    els.preview.innerHTML = `<iframe title="Prévia do arquivo" src="${escapeHtml(item.drive_preview_url)}"></iframe>`;
    return;
  }

  if (item.doc) {
    let aviso = "Artigo destacado no texto completo da norma. Trechos riscados são redação revogada.\n\n";
    if (needles.length) aviso += "Termos da busca destacados em amarelo.\n\n";
    els.preview.innerHTML = `<div class="text-preview">${escapeHtml(aviso)}${textoComDispositivo(item, needles)}</div>`;
    scrollToFirstHighlight(".dispositivo-atual");
    return;
  }

  const text = item.texto || "Texto não disponível para este documento.";
  let notice = "";
  if (item.ocr) {
    notice = "Texto reconhecido por OCR (documento escaneado); pode conter erros. Use “Arquivo” para ver o original do Drive.\n\n";
  } else if (item.tem_arquivo_publico) {
    notice = "Prévia textual extraída do documento. Use “Arquivo” para ver o original do Drive ou “Abrir inteiro” para abrir em nova aba.\n\n";
  }
  if (needles.length) {
    notice += "Termos da busca destacados em amarelo.\n\n";
  }
  els.preview.innerHTML = `<div class="text-preview">${escapeHtml(notice)}${highlight(text, needles)}</div>`;
  scrollToFirstHighlight();
}

// Documento inteiro com o artigo em destaque, a redação revogada riscada e os termos
// da busca marcados só dentro do artigo.
function textoComDispositivo(item, needles) {
  const texto = item.doc.texto;
  const riscado = item.doc.riscado || [];
  const pedaco = (inicio, fim, destacar) =>
    destacar ? highlight(texto.slice(inicio, fim), needles) : escapeHtml(texto.slice(inicio, fim));
  const trecho = (inicio, fim, destacar) => {
    let html = "";
    let pos = inicio;
    for (const [a, b] of riscado) {
      if (b <= pos) continue;
      if (a >= fim) break;
      if (a > pos) html += pedaco(pos, a, destacar);
      html += `<s class="revogado">${pedaco(Math.max(a, pos), Math.min(b, fim), destacar)}</s>`;
      pos = Math.min(b, fim);
    }
    if (pos < fim) html += pedaco(pos, fim, destacar);
    return html;
  };
  // As quebras de linha em volta ficam por conta do bloco destacado.
  let antes = item.inicio;
  while (antes > 0 && texto[antes - 1] === "\n") antes -= 1;
  let depois = item.fim;
  while (depois < texto.length && texto[depois] === "\n") depois += 1;
  return (
    trecho(0, antes, false) +
    `<div class="dispositivo-atual">${trecho(item.inicio, item.fim, true)}</div>` +
    trecho(depois, texto.length, false)
  );
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

els.similares?.addEventListener("click", (event) => {
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
