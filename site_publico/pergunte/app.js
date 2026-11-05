// Pergunte ao Entendimento: envia a pergunta para /api/perguntar e mostra a resposta, com as
// citações [n] ligadas ao texto dos tópicos consultados. Sem a API (ex.: GitHub Pages), avisa.
(() => {
  const API_BASE = (window.JURIS_CONFIG?.API_BASE ?? "").replace(/\/$/, "");
  const SITE_COM_API = "https://omniailab.com.br/site_publico/pergunte/";

  const form = document.querySelector("#pergunta-form");
  const input = document.querySelector("#pergunta-input");
  const enviar = document.querySelector("#pergunta-enviar");
  const secao = document.querySelector("#resposta");
  const carregando = document.querySelector("#resposta-carregando");
  const aviso = document.querySelector("#resposta-aviso");
  const texto = document.querySelector("#resposta-texto");
  const modelo = document.querySelector("#resposta-modelo");
  const fontesBloco = document.querySelector("#fontes-bloco");
  const fontes = document.querySelector("#fontes");
  const documentoLink = document.querySelector("#documento-link");

  let pedidoAtual = null;

  function escapar(valor) {
    return String(valor).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  // Parágrafos, **negrito** e [n] como link para o tópico (o texto já vem escapado).
  function formatar(resposta, total) {
    return escapar(resposta)
      .split(/\n\s*\n/)
      .map((p) => p.trim())
      .filter(Boolean)
      .map((p) => {
        const html = p
          .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
          .replace(/\n/g, "<br />")
          .replace(/\[(\d+)\]/g, (m, n) =>
            Number(n) >= 1 && Number(n) <= total
              ? `<a class="citacao" href="#fonte-${n}" data-fonte="${n}" aria-label="Tópico ${n}">[${n}]</a>`
              : m,
          );
        return `<p>${html}</p>`;
      })
      .join("");
  }

  function mostrar(el, conteudo) {
    el.hidden = !conteudo;
    if (conteudo !== undefined) el.innerHTML = conteudo || "";
  }

  function limpar() {
    secao.hidden = false;
    mostrar(aviso, "");
    mostrar(texto, "");
    mostrar(modelo, "");
    fontesBloco.hidden = true;
    fontes.innerHTML = "";
  }

  function renderizarFontes(lista) {
    fontes.innerHTML = lista
      .map((f, i) => {
        const partes = f.titulo.split(" › ");
        const folha = partes.pop();
        const caminho = partes.join(" › ");
        const corpo = escapar(f.texto)
          .split(/\n\s*\n/)
          .map((p) => `<p>${p}</p>`)
          .join("");
        return `<details class="fonte" id="fonte-${f.n}"${i === 0 ? " open" : ""}>
          <summary><span class="fonte-n">${f.n}</span><span class="fonte-titulo"><strong>${escapar(folha)}</strong>${
            caminho ? `<span class="fonte-caminho">${escapar(caminho)}</span>` : ""
          }</span></summary>
          <div class="fonte-texto prose">${corpo}</div>
        </details>`;
      })
      .join("");
    fontesBloco.hidden = lista.length === 0;
  }

  function renderizar(dados) {
    limpar();
    if (dados.aviso) mostrar(aviso, escapar(dados.aviso));
    if (dados.resposta) mostrar(texto, formatar(dados.resposta, dados.fontes.length));
    if (dados.modelo) mostrar(modelo, `Resposta escrita por IA (${escapar(dados.modelo)}) a partir dos tópicos abaixo.`);
    renderizarFontes(dados.fontes || []);
    if (dados.documento?.link) {
      documentoLink.href = dados.documento.link;
      documentoLink.parentElement.hidden = false;
    } else {
      documentoLink.parentElement.hidden = true;
    }
  }

  function erro(mensagem) {
    limpar();
    mostrar(aviso, mensagem);
  }

  async function perguntar(pergunta) {
    pedidoAtual?.abort();
    const controle = new AbortController();
    pedidoAtual = controle;
    const tempo = setTimeout(() => controle.abort(), 30000);

    limpar();
    carregando.hidden = false;
    secao.setAttribute("aria-busy", "true");
    enviar.disabled = true;
    try {
      const url = `${API_BASE}/api/perguntar?${new URLSearchParams({ q: pergunta })}`;
      const resposta = await fetch(url, { signal: controle.signal, headers: { Accept: "application/json" } });
      const tipo = resposta.headers.get("content-type") || "";
      if (!tipo.includes("application/json")) {
        // Site estático (GitHub Pages): não há API para responder.
        erro(`As perguntas precisam do servidor do site. Use em <a href="${SITE_COM_API}">omniailab.com.br</a>.`);
        return;
      }
      const dados = await resposta.json();
      if (resposta.status === 429) {
        erro("Muitas perguntas ao mesmo tempo. Aguarde alguns segundos e tente de novo.");
      } else if (!resposta.ok) {
        erro(escapar(dados?.erro?.mensagem || "Não foi possível responder agora. Tente de novo em instantes."));
      } else {
        renderizar(dados);
      }
    } catch (e) {
      if (controle !== pedidoAtual) return; // substituída por outra pergunta
      erro(
        e.name === "AbortError"
          ? "A resposta demorou demais. Tente de novo em instantes."
          : "Sem conexão com o servidor. Confira a internet e tente de novo.",
      );
    } finally {
      clearTimeout(tempo);
      if (controle === pedidoAtual) {
        carregando.hidden = true;
        secao.setAttribute("aria-busy", "false");
        enviar.disabled = false;
      }
    }
  }

  form.addEventListener("submit", (evento) => {
    evento.preventDefault();
    const pergunta = input.value.trim();
    if (pergunta.length < 3) {
      input.focus();
      return;
    }
    const params = new URLSearchParams(location.search);
    params.set("q", pergunta);
    history.replaceState(null, "", `${location.pathname}?${params}`);
    perguntar(pergunta);
  });

  for (const botao of document.querySelectorAll("[data-pergunta]")) {
    botao.addEventListener("click", () => {
      input.value = botao.dataset.pergunta;
      form.requestSubmit();
    });
  }

  // Clicar em [n] abre o tópico e rola até ele.
  texto.addEventListener("click", (evento) => {
    const link = evento.target.closest("[data-fonte]");
    if (!link) return;
    const alvo = document.querySelector(`#fonte-${link.dataset.fonte}`);
    if (alvo) alvo.open = true;
  });

  // Link compartilhável: ?q=pergunta já responde ao abrir.
  const inicial = new URLSearchParams(location.search).get("q");
  if (inicial) {
    input.value = inicial;
    perguntar(inicial.trim());
  }
})();
