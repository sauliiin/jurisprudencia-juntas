// IA do site: modelo treinado do zero com o acervo (treinar_ia.py), rodando no navegador.
//
// Arquivos em site_data/ia/ (pesos em 8 bits):
//   modelo.json       vocabulário, idf, ids das decisões, classes e métricas
//   vizinhos.bin      Uint16 [n_docs x n_vizinhos]  casos semelhantes, já calculados
//   resultados.bin    Uint8  [n_docs]               resultado de cada decisão
//   termos.bin        Int8   [n_termos x dim]       1ª camada da rede
//   docs.bin          Int8   [n_docs x dim]         vetor de cada decisão
//   classificador.bin Float32 [dim x classes + classes]  última camada
//
// Os dois primeiros binários são pequenos e bastam para "casos semelhantes"; os
// demais só são baixados quando alguém usa a busca por significado.
//
// Legislação (modo Legislação), no mesmo espaço do modelo:
//   legislacao.json          ids dos artigos/itens vigentes, citações e métricas
//   legislacao_vizinhos.bin  Uint16 [n x n_vizinhos]  artigos semelhantes, já calculados
//   legislacao_docs.bin      Int8   [n x dim]          vetor de cada artigo
const IA = (() => {
  // Relativo a este arquivo, para servir tanto site_publico/ quanto site_publico/legislacao/.
  const BASE =
    typeof document !== "undefined" && document.currentScript
      ? new URL("../site_data/ia/", document.currentScript.src).href
      : "../site_data/ia/";
  let leve = null;
  let completo = null;
  let legislacao = null;
  let legislacaoVetores = null;

  async function binario(nome, Tipo) {
    const resposta = await fetch(BASE + nome);
    if (!resposta.ok) throw new Error(`IA indisponível (${nome}: HTTP ${resposta.status})`);
    return new Tipo(await resposta.arrayBuffer());
  }

  function carregarLeve() {
    leve ??= (async () => {
      const resposta = await fetch(BASE + "modelo.json");
      if (!resposta.ok) throw new Error(`IA indisponível (modelo.json: HTTP ${resposta.status})`);
      const modelo = await resposta.json();
      const [vizinhos, resultados] = await Promise.all([
        binario("vizinhos.bin", Uint16Array),
        binario("resultados.bin", Uint8Array),
      ]);
      const indice = new Map(modelo.ids.map((id, i) => [id, i]));
      return { modelo, vizinhos, resultados, indice };
    })().catch((erro) => {
      leve = null;
      throw erro;
    });
    return leve;
  }

  function carregarCompleto() {
    completo ??= (async () => {
      const base = await carregarLeve();
      const [termos, docs, classificador] = await Promise.all([
        binario("termos.bin", Int8Array),
        binario("docs.bin", Int8Array),
        binario("classificador.bin", Float32Array),
      ]);
      const vocab = new Map(base.modelo.vocab.map((termo, i) => [termo, i]));
      return { ...base, termos, docs, classificador, vocab };
    })().catch((erro) => {
      completo = null;
      throw erro;
    });
    return completo;
  }

  function carregarLegislacao() {
    legislacao ??= (async () => {
      const resposta = await fetch(BASE + "legislacao.json");
      if (!resposta.ok) throw new Error(`IA indisponível (legislacao.json: HTTP ${resposta.status})`);
      const modelo = await resposta.json();
      const vizinhos = await binario("legislacao_vizinhos.bin", Uint16Array);
      const indice = new Map(modelo.ids.map((id, i) => [id, i]));
      return { modelo, vizinhos, indice };
    })().catch((erro) => {
      legislacao = null;
      throw erro;
    });
    return legislacao;
  }

  function carregarLegislacaoVetores() {
    legislacaoVetores ??= (async () => {
      const [leg, m, docs] = await Promise.all([
        carregarLegislacao(),
        carregarCompleto(),
        binario("legislacao_docs.bin", Int8Array),
      ]);
      return { leg, m, docs };
    })().catch((erro) => {
      legislacaoVetores = null;
      throw erro;
    });
    return legislacaoVetores;
  }

  // Precisa ser idêntico ao tokenizador de treinar_ia.py: minúsculas, sem acentos,
  // caracteres não-ASCII descartados, só sequências de 3 ou mais letras.
  function tokenizar(texto) {
    return (
      String(texto)
        .toLowerCase()
        .normalize("NFD")
        .replace(/[^\x00-\x7f]/g, "")
        .match(/[a-z]{3,}/g) || []
    );
  }

  // TF-IDF da consulta, projetado pela 1ª camada: o mesmo caminho que cada decisão fez no treino.
  function vetorizar(m, texto) {
    const { dim, idf } = m.modelo;
    const contagem = new Map();
    for (const token of tokenizar(texto)) {
      const j = m.vocab.get(token);
      if (j !== undefined) contagem.set(j, (contagem.get(j) || 0) + 1);
    }
    const pesos = [];
    let norma = 0;
    for (const [j, n] of contagem) {
      const peso = (1 + Math.log(n)) * idf[j];
      pesos.push([j, peso]);
      norma += peso * peso;
    }
    norma = Math.sqrt(norma) || 1;
    const vetor = new Float32Array(dim);
    for (const [j, peso] of pesos) {
      const p = peso / norma;
      const base = j * dim;
      for (let k = 0; k < dim; k += 1) vetor[k] += p * m.termos[base + k];
    }
    return { vetor, termosConhecidos: pesos.length };
  }

  function classificar(m, vetor) {
    const { dim, classes, escala_termos: escala } = m.modelo;
    const n = classes.length;
    const logits = new Float32Array(n);
    for (let c = 0; c < n; c += 1) logits[c] = m.classificador[dim * n + c];
    for (let k = 0; k < dim; k += 1) {
      const h = (vetor[k] * escala) / 127;
      const base = k * n;
      for (let c = 0; c < n; c += 1) logits[c] += h * m.classificador[base + c];
    }
    let melhor = 0;
    for (let c = 1; c < n; c += 1) if (logits[c] > logits[melhor]) melhor = c;
    let soma = 0;
    for (let c = 0; c < n; c += 1) soma += Math.exp(logits[c] - logits[melhor]);
    return { nome: classes[melhor], probabilidade: 1 / soma };
  }

  /** Similaridade (cosseno, -1 a 1) da consulta com cada decisão, na ordem de ids(). */
  async function buscar(texto) {
    const m = await carregarCompleto();
    const { dim, n_docs: nDocs } = m.modelo;
    const { vetor, termosConhecidos } = vetorizar(m, texto);
    let norma = 0;
    for (let k = 0; k < dim; k += 1) norma += vetor[k] * vetor[k];
    norma = Math.sqrt(norma);
    const similaridades = new Float32Array(nDocs);
    if (!norma) return { ids: m.modelo.ids, similaridades, infracao: null, termosConhecidos };
    for (let i = 0; i < nDocs; i += 1) {
      const base = i * dim;
      let soma = 0;
      for (let k = 0; k < dim; k += 1) soma += m.docs[base + k] * vetor[k];
      similaridades[i] = soma / (127 * norma);
    }
    return { ids: m.modelo.ids, similaridades, infracao: classificar(m, vetor), termosConhecidos };
  }

  /** Decisões mais próximas de uma decisão do acervo: [{ id, resultado }]. */
  async function semelhantes(fileId, quantos = 5) {
    const m = await carregarLeve();
    const i = m.indice.get(fileId);
    if (i === undefined) return [];
    const k = m.modelo.n_vizinhos;
    const saida = [];
    for (let v = 0; v < Math.min(quantos, k); v += 1) {
      const j = m.vizinhos[i * k + v];
      saida.push({ id: m.modelo.ids[j], resultado: m.modelo.resultados[m.resultados[j]] });
    }
    return saida;
  }

  /** Similaridade da consulta com cada artigo/item vigente da legislação, na ordem de ids. */
  async function buscarLegislacao(texto) {
    const { leg, m, docs } = await carregarLegislacaoVetores();
    const { dim, n_docs: n } = leg.modelo;
    const { vetor, termosConhecidos } = vetorizar(m, texto);
    let norma = 0;
    for (let k = 0; k < dim; k += 1) norma += vetor[k] * vetor[k];
    norma = Math.sqrt(norma);
    const similaridades = new Float32Array(n);
    if (norma) {
      for (let i = 0; i < n; i += 1) {
        const base = i * dim;
        let soma = 0;
        for (let k = 0; k < dim; k += 1) soma += docs[base + k] * vetor[k];
        similaridades[i] = soma / (127 * norma);
      }
    }
    return { ids: leg.modelo.ids, similaridades, termosConhecidos };
  }

  /** Artigos mais próximos de um artigo da legislação: [id, ...]. */
  async function artigosSemelhantes(id, quantos = 5) {
    const { modelo, vizinhos, indice } = await carregarLegislacao();
    const i = indice.get(id);
    if (i === undefined) return [];
    const k = modelo.n_vizinhos;
    const saida = [];
    for (let v = 0; v < Math.min(quantos, k); v += 1) saida.push(modelo.ids[vizinhos[i * k + v]]);
    return saida;
  }

  /** Quantas decisões do acervo têm auto que cita o artigo (null se a IA não o conhece). */
  async function citacoesDoArtigo(id) {
    const { modelo, indice } = await carregarLegislacao();
    const i = indice.get(id);
    return i === undefined ? null : modelo.citacoes[i];
  }

  async function avaliacaoLegislacao() {
    const { modelo } = await carregarLegislacao();
    return { ...modelo.avaliacao, gerado_em: modelo.gerado_em, n_docs: modelo.n_docs };
  }

  /** Resultado de cada decisão lido do texto no treino: Map id -> "deferido", "indeferido"... */
  let mapaResultados = null;
  async function resultados() {
    mapaResultados ??= carregarLeve().then(({ modelo, resultados: codigos }) => {
      const mapa = new Map();
      modelo.ids.forEach((id, i) => {
        const rotulo = modelo.resultados[codigos[i]];
        if (rotulo) mapa.set(id, rotulo);
      });
      return mapa;
    }).catch((erro) => {
      mapaResultados = null;
      throw erro;
    });
    return mapaResultados;
  }

  async function avaliacao() {
    const m = await carregarLeve();
    return { ...m.modelo.avaliacao, gerado_em: m.modelo.gerado_em, n_docs: m.modelo.n_docs };
  }

  return {
    buscar,
    semelhantes,
    resultados,
    avaliacao,
    tokenizar,
    buscarLegislacao,
    artigosSemelhantes,
    citacoesDoArtigo,
    avaliacaoLegislacao,
  };
})();

if (typeof module !== "undefined") module.exports = IA;
