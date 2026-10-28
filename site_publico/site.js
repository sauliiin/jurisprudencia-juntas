// Comportamento comum a todas as páginas: tema (claro, escuro, cyberpunk), menu "Mais", botão de
// filtros e, no celular, o detalhe em tela cheia com "Voltar". Não toca na busca.
(() => {
  const raiz = document.documentElement;
  const CHAVE_TEMA = "omniai-tema";
  const celular = window.matchMedia("(max-width: 860px)");

  // O botão gira claro → escuro → cyberpunk; sem escolha salva, vale o tema do sistema.
  const TEMAS = ["light", "dark", "cyberpunk"];
  const NOMES = { light: "claro", dark: "escuro", cyberpunk: "cyberpunk" };

  function temaAtual() {
    if (TEMAS.includes(raiz.dataset.theme)) return raiz.dataset.theme;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function proximoTema() {
    return TEMAS[(TEMAS.indexOf(temaAtual()) + 1) % TEMAS.length];
  }

  function rotularTema(botao) {
    botao.setAttribute("aria-label", `Tema ${NOMES[temaAtual()]}; mudar para ${NOMES[proximoTema()]}`);
    botao.title = `Mudar para o tema ${NOMES[proximoTema()]}`;
  }

  for (const botao of document.querySelectorAll(".tema-toggle")) {
    rotularTema(botao);
    botao.addEventListener("click", () => {
      const novo = proximoTema();
      raiz.dataset.theme = novo;
      try {
        localStorage.setItem(CHAVE_TEMA, novo);
      } catch {
        // Sem armazenamento (modo privado): o tema vale só nesta página.
      }
      rotularTema(botao);
    });
  }

  // Menu "Mais": fecha ao clicar fora, com Esc e ao escolher um item.
  for (const menu of document.querySelectorAll("details.menu")) {
    document.addEventListener("click", (evento) => {
      if (menu.open && !menu.contains(evento.target)) menu.open = false;
    });
    menu.addEventListener("keydown", (evento) => {
      if (evento.key === "Escape" && menu.open) {
        menu.open = false;
        menu.querySelector("summary")?.focus();
      }
    });
  }

  const toggle = document.querySelector(".filtros-toggle");
  const filtros = document.querySelector("#filtros");
  if (toggle && filtros) {
    toggle.addEventListener("click", () => {
      const aberto = filtros.classList.toggle("aberto");
      toggle.setAttribute("aria-expanded", String(aberto));
    });
  }

  // Celular: o detalhe abre por cima da lista. Abre quando um resultado é escolhido
  // (o título do detalhe muda) e fecha com "Voltar", Esc ou o botão voltar do navegador.
  const detalhe = document.querySelector("#detail");
  const titulo = document.querySelector("#detail-title");
  if (!detalhe || !titulo) return;
  let comHistorico = false;

  function abrir() {
    if (!celular.matches || detalhe.hidden) return;
    document.body.classList.add("detalhe-aberto");
    document.querySelector(".detail-pane")?.scrollTo(0, 0);
    if (!comHistorico) {
      history.pushState({ detalhe: true }, "");
      comHistorico = true;
    }
    document.querySelector("[data-voltar]")?.focus({ preventScroll: true });
  }

  function fechar({ doHistorico = false } = {}) {
    if (!document.body.classList.contains("detalhe-aberto")) return;
    document.body.classList.remove("detalhe-aberto");
    if (comHistorico && !doHistorico) history.back();
    comHistorico = false;
    document.querySelector(".result.active")?.focus({ preventScroll: false });
  }

  new MutationObserver(abrir).observe(titulo, { childList: true, characterData: true, subtree: true });
  for (const botao of document.querySelectorAll("[data-voltar]")) botao.addEventListener("click", () => fechar());
  window.addEventListener("popstate", () => fechar({ doHistorico: true }));
  document.addEventListener("keydown", (evento) => {
    if (evento.key === "Escape") fechar();
  });
  celular.addEventListener("change", () => {
    if (!celular.matches) fechar();
  });
})();
