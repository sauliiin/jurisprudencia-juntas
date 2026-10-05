#!/usr/bin/env python3
"""
Treina, do zero, a IA do site público e exporta os arquivos que o navegador usa.

    python3 -u treinar_ia.py

É uma rede neural pequena, treinada só com o acervo (site_data/votos.jsonl):

    texto -> TF-IDF -> camada de 128 dimensões -> infração do auto (gabarito do SIF)

A camada do meio é o que interessa: decisões sobre a mesma infração ficam
próximas nesse espaço de 128 dimensões. O site usa isso para:

  - "Casos semelhantes": os vizinhos mais próximos de cada decisão;
  - "Busca por significado": a consulta é levada ao mesmo espaço e comparada
    com todas as decisões;
  - "Infração provável": a última camada, aplicada ao texto digitado.

Tudo roda no navegador, sem servidor: o treino acontece aqui e o resultado vai
para site_data/ia/ (pesos compactados em 8 bits). A cada execução o modelo é
refeito com o acervo atual; é assim que ele "aprende" com as decisões novas.

Antes de treinar o modelo final, o script separa 20% das decisões, treina nos
outros 80% e mede a qualidade nas que ficaram de fora. Os números vão para
site_data/ia/modelo.json e aparecem na página de instruções.

Legislação (site_data/legislacao.jsonl, dividida em artigos por legislacao.py):
cada artigo vigente vai para o mesmo espaço de 128 dimensões, somado à média das
decisões cujos autos o citam no "dispositivo legal transgredido". Assim um artigo
fica perto das infrações em que é aplicado, e não só das palavras que contém. Daí
saem a busca por significado e os "artigos semelhantes" do modo Legislação. A
medição usa o mesmo corte: a partir do relato do fiscal ou da descrição da
infração de uma decisão nunca vista, o artigo citado no auto aparece no topo?
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import random
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import numpy as np
import scipy.sparse as sp

from legislacao import chave_norma, citacoes

RAIZ = Path(__file__).resolve().parent
VOTOS = RAIZ / "site_data" / "votos.jsonl"
LEGISLACAO = RAIZ / "site_data" / "legislacao.jsonl"
SAIDA = RAIZ / "site_data" / "ia"

DIM = 128
EPOCAS = 20
LOTE = 256
TAXA = 2e-3
VIZINHOS = 8
MAX_TERMOS = 30000
MIN_DOCS_POR_INFRACAO = 5
SEMENTE = 7
# Peso da média das decisões que citam o artigo, somada ao vetor do próprio texto.
# Medido em decisões nunca vistas: 1 equilibra a busca pelo relato e pela infração.
PESO_CITACOES = 1.0

# O tokenizador precisa ser idêntico ao de site_publico/ia.js: minúsculas, sem
# acentos, caracteres não-ASCII descartados, só sequências de 3+ letras.
_TOKEN = re.compile(r"[a-z]{3,}")
_NAO_ASCII = re.compile(r"[^\x00-\x7f]")
_AUTO = re.compile(r"\b\d{8,14}[A-Z]{2}\b")
_FISCAL = re.compile(
    r"considera[çc][õo]es fiscais\s*:?\s*(.+?)(?:Em sua defesa|FUNDAMENTA[ÇC][ÃA]O|$)", re.I | re.S
)

RESULTADOS = ["", "indeferido", "deferido", "parcialmente deferido", "não conhecido", "extinto", "diligência"]
_PADROES_RESULTADO = [
    (5, re.compile(r"EXTINT[OA]|PERDA\s+DO\s+OBJETO")),
    (4, re.compile(r"NAO\s+(?:SE\s+)?CONHEC")),
    (3, re.compile(r"PARCIALMENTE\s+DEFERID|DEFIRO\s+PARCIALMENTE|DEFIRO\s+EM\s+PARTE|PARCIAL\s+PROVIMENTO|DEFERIMENTO\s+PARCIAL")),
    (1, re.compile(r"INDEFIR|INDEFERID|NEGO\s+PROVIMENTO|NEGAR\s+PROVIMENTO|IMPROCEDENTE")),
    (2, re.compile(r"\bDEFIR|\bDEFERID|DOU\s+PROVIMENTO|DAR\s+PROVIMENTO|\bPROCEDENTE")),
    (6, re.compile(r"DILIGENCIA")),
]


def normalizar(texto: str) -> str:
    return _NAO_ASCII.sub("", unicodedata.normalize("NFD", texto.lower()))


def tokenizar(texto: str) -> list[str]:
    return _TOKEN.findall(normalizar(texto))


def chave_infracao(infracao: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", normalizar(infracao or ""))).strip()[:160]


def resultado_da_decisao(texto: str) -> int:
    """Código em RESULTADOS, lido do trecho 'Dispositivo da decisão' (última ocorrência)."""
    plano = normalizar(texto).upper()
    pos = plano.rfind("DISPOSITIVO DA DECIS")
    if pos < 0:
        return 0
    trecho = plano[pos : pos + 700]
    for codigo, padrao in _PADROES_RESULTADO:
        if padrao.search(trecho):
            return codigo
    return 0


def carregar_votos() -> list[dict]:
    with VOTOS.open(encoding="utf-8") as fh:
        return [json.loads(linha) for linha in fh if linha.strip()]


def montar_vocabulario(docs: list[list[str]]) -> tuple[list[str], np.ndarray]:
    """Termos que aparecem em pelo menos 3 decisões e em no máximo metade delas."""
    n = len(docs)
    df = collections.Counter(t for doc in docs for t in set(doc))
    vocab = sorted((t for t, c in df.items() if 3 <= c <= 0.5 * n), key=lambda t: (-df[t], t))[:MAX_TERMOS]
    idf = np.array([math.log((1 + n) / (1 + df[t])) + 1 for t in vocab], dtype=np.float32)
    return vocab, idf


def tfidf(listas: list[list[str]], indice: dict[str, int], idf: np.ndarray) -> sp.csr_matrix:
    """(1 + ln tf) * idf, com cada linha normalizada para norma 1."""
    linhas, colunas, valores = [], [], []
    for i, tokens in enumerate(listas):
        contagem = collections.Counter(indice[t] for t in tokens if t in indice)
        pesos = {j: (1 + math.log(n)) * float(idf[j]) for j, n in contagem.items()}
        norma = math.sqrt(sum(p * p for p in pesos.values())) or 1.0
        for j, p in pesos.items():
            linhas.append(i)
            colunas.append(j)
            valores.append(p / norma)
    return sp.csr_matrix((valores, (linhas, colunas)), shape=(len(listas), len(indice)), dtype=np.float32)


def treinar(x: sp.csr_matrix, rotulos: np.ndarray, n_classes: int, dim: int, epocas: int) -> tuple:
    """Rede de uma camada oculta linear, entropia cruzada e Adam, tudo em NumPy."""
    rng = np.random.default_rng(SEMENTE)
    w = rng.normal(0, 0.05, (x.shape[1], dim)).astype(np.float32)
    c = rng.normal(0, 0.05, (dim, n_classes)).astype(np.float32)
    b = np.zeros(n_classes, dtype=np.float32)
    params = [w, c, b]
    m = [np.zeros_like(p) for p in params]
    v = [np.zeros_like(p) for p in params]
    passo = 0
    for epoca in range(epocas):
        ordem = rng.permutation(x.shape[0])
        perda = 0.0
        for ini in range(0, len(ordem), LOTE):
            idx = ordem[ini : ini + LOTE]
            xb, yb = x[idx], rotulos[idx]
            h = xb @ w
            z = h @ c + b
            z -= z.max(axis=1, keepdims=True)
            p = np.exp(z)
            p /= p.sum(axis=1, keepdims=True)
            perda += float(-np.log(p[np.arange(len(idx)), yb] + 1e-9).sum())
            p[np.arange(len(idx)), yb] -= 1
            p /= len(idx)
            grads = (np.asarray(xb.T @ (p @ c.T)) + 1e-5 * w, h.T @ p, p.sum(axis=0))
            passo += 1
            for param, g, mi, vi in zip(params, grads, m, v, strict=True):
                mi *= 0.9
                mi += 0.1 * g
                vi *= 0.999
                vi += 0.001 * g * g
                param -= TAXA * (mi / (1 - 0.9**passo)) / (np.sqrt(vi / (1 - 0.999**passo)) + 1e-8)
        print(f"  época {epoca + 1:2d}/{epocas} · perda média {perda / x.shape[0]:.4f}", flush=True)
    return w, c, b


def quantizar(matriz: np.ndarray) -> tuple[np.ndarray, float]:
    """Pesos em 8 bits: o navegador baixa 4x menos dados, sem perda mensurável de qualidade."""
    escala = float(np.abs(matriz).max()) or 1.0
    return np.round(matriz / escala * 127).astype(np.int8), escala


def vetores_de_documentos(x: sp.csr_matrix, w_q: np.ndarray) -> np.ndarray:
    """Vetores normalizados, calculados com os pesos já quantizados (como o navegador fará)."""
    d = np.asarray(x @ w_q.astype(np.float32))
    d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
    return d


def carregar_legislacao() -> list[dict]:
    """Artigos e itens vigentes da legislação, com a chave (norma, artigo) quando é artigo."""
    if not LEGISLACAO.exists():
        return []
    unidades = []
    with LEGISLACAO.open(encoding="utf-8") as fh:
        for linha in fh:
            if not linha.strip():
                continue
            doc = json.loads(linha)
            norma = re.match(r"(Lei|Decreto) ([\d.]+)$", doc.get("categoria") or "")
            texto = doc.get("texto") or ""
            for u in doc.get("dispositivos") or []:
                if u["revogado"]:
                    continue
                chave = (chave_norma(*norma.groups()), u["numero"]) if norma and u["tipo"] == "artigo" else None
                unidades.append({
                    "id": f"{doc['file_id']}#{u['id']}",
                    "doc": doc["file_id"],
                    "numero": u["numero"],
                    "chave": chave,
                    "texto": texto[u["inicio"] : u["fim"]],
                })
    return unidades


def citacoes_por_decisao(votos: list[dict]) -> list[set]:
    """Artigos citados nos autos de cada decisão (campo 'dispositivo legal transgredido')."""
    return [
        set().union(*(citacoes(a.get("dispositivo_legal_transgredido") or "") for a in v.get("autos") or []))
        for v in votos
    ]


def vetores_legislacao(unidades, x_unid, citantes: dict, d: np.ndarray, w_q: np.ndarray) -> np.ndarray:
    """Vetor do texto do artigo + PESO_CITACOES x média das decisões que o citam."""
    u = vetores_de_documentos(x_unid, w_q)
    for j, unidade in enumerate(unidades):
        idx = citantes.get(unidade["chave"])
        if idx:
            media = d[idx].mean(axis=0)
            u[j] += PESO_CITACOES * media / max(float(np.linalg.norm(media)), 1e-9)
    u /= np.maximum(np.linalg.norm(u, axis=1, keepdims=True), 1e-9)
    return u


def _citantes(citadas: list[set], excluir: set[int] = frozenset()) -> dict:
    saida = collections.defaultdict(list)
    for i, chaves in enumerate(citadas):
        if i not in excluir:
            for chave in chaves:
                saida[chave].append(i)
    return saida


def avaliar_legislacao(votos, teste, citadas, unidades, x_unid, d, w_q, vocab_idx, idf) -> dict:
    """Do relato do fiscal (ou da descrição da infração) de uma decisão nunca vista,
    o artigo citado no auto aparece entre os primeiros? Só artigos entram no ranking."""
    artigos = [j for j, u in enumerate(unidades) if u["chave"]]
    chaves_artigos = {unidades[j]["chave"] for j in artigos}
    teste_set = set(teste)
    u = vetores_legislacao(unidades, x_unid, _citantes(citadas, teste_set), d, w_q)[artigos]

    # Comparação: busca por palavras (TF-IDF com o vocabulário dos próprios artigos).
    tokens = [tokenizar(unidades[j]["texto"]) for j in artigos]
    df = collections.Counter(t for tk in tokens for t in set(tk))
    lex_idx = {t: n for n, t in enumerate(sorted(df))}
    lex_idf = np.array([math.log((1 + len(tokens)) / (1 + df[t])) + 1 for t in sorted(df)], dtype=np.float32)
    lex = tfidf(tokens, lex_idx, lex_idf)

    def medir(consultas: list[tuple[int, str]], palavras: bool = False) -> tuple[float, float]:
        textos = [tokenizar(t) for _, t in consultas]
        if palavras:
            sim = (tfidf(textos, lex_idx, lex_idf) @ lex.T).toarray()
        else:
            sim = vetores_de_documentos(tfidf(textos, vocab_idx, idf), w_q) @ u.T
        acertos5 = rr = 0.0
        for r, (i, _) in enumerate(consultas):
            ordem = np.argsort(-sim[r])[:10]
            pos = next((n for n, j in enumerate(ordem) if unidades[artigos[j]]["chave"] in citadas[i]), None)
            if pos is not None:
                rr += 1 / (pos + 1)
                acertos5 += pos < 5
        return round(acertos5 / len(consultas), 3), round(rr / len(consultas), 3)

    infracoes, relatos = [], []
    for i in teste:
        if not citadas[i] & chaves_artigos:
            continue
        infracao = (votos[i]["autos"][0].get("infracao") or "").strip()
        if infracao:
            infracoes.append((i, infracao))
        achado = _FISCAL.search(votos[i].get("texto") or "")
        if achado:
            consulta = _AUTO.sub("", achado.group(1))[:600]
            if len(consulta) >= 60:
                relatos.append((i, consulta))
    if not infracoes or not relatos:
        return {}
    inf5, inf_mrr = medir(infracoes)
    rel5, rel_mrr = medir(relatos)
    rel5_palavras, _ = medir(relatos, palavras=True)
    return {
        "artigos": len(artigos),
        "consultas_infracao": len(infracoes),
        "infracao_top5": inf5,
        "infracao_mrr10": inf_mrr,
        "consultas_relato": len(relatos),
        "relato_top5": rel5,
        "relato_mrr10": rel_mrr,
        "relato_top5_so_palavras": rel5_palavras,
    }


def avaliar(votos, x, chaves, elegiveis, vocab_idx, idf, dim, epocas, leg=None) -> tuple[dict, dict]:
    """Treina em 80% das decisões rotuladas e mede nas 20% que ficaram de fora.

    leg = (citadas, unidades, x_unid) mede também a legislação com o mesmo modelo.
    """
    rng = random.Random(SEMENTE)
    embaralhados = elegiveis[:]
    rng.shuffle(embaralhados)
    corte = int(0.8 * len(embaralhados))
    treino, teste = embaralhados[:corte], embaralhados[corte:]
    classes = sorted({next(iter(chaves[i])) for i in treino})
    indice = {k: n for n, k in enumerate(classes)}
    rotulos = np.array([indice[next(iter(chaves[i]))] for i in treino])
    print(f"Avaliação: treino em {len(treino)} decisões, teste em {len(teste)} nunca vistas", flush=True)
    w, c, b = treinar(x[treino], rotulos, len(classes), dim, epocas)
    w_q, escala = quantizar(w)
    d = vetores_de_documentos(x, w_q)

    # Casos semelhantes: dos 5 vizinhos de cada decisão de teste, quantos têm a mesma infração?
    sim = d[teste] @ d.T
    sim[np.arange(len(teste)), teste] = -9
    top = np.argpartition(-sim, 5, axis=1)[:, :5]
    p5 = float(np.mean([np.mean([next(iter(chaves[i])) in chaves[j] for j in top[r]]) for r, i in enumerate(teste)]))
    acaso = float(np.mean([(sum(next(iter(chaves[i])) in ks for ks in chaves) - 1) / (len(votos) - 1) for i in teste[:300]]))

    # Classificação da infração a partir do texto da decisão.
    logits = (np.asarray(x[teste] @ w_q.astype(np.float32)) * (escala / 127)) @ c + b
    acerto = float(np.mean([classes[j] in chaves[i] for i, j in zip(teste, logits.argmax(axis=1), strict=True)]))

    # Busca por significado: a narrativa do fiscal (sem o nome da infração) acha decisões da mesma infração?
    narrativas = []
    for i in teste:
        achado = _FISCAL.search(votos[i].get("texto") or "")
        if achado:
            consulta = _AUTO.sub("", achado.group(1))[:600]
            if len(consulta) >= 60:
                narrativas.append((i, consulta))
    q = vetores_de_documentos(tfidf([tokenizar(t) for _, t in narrativas], vocab_idx, idf), w_q)
    sim = q @ d.T
    rr = []
    for r, (i, _) in enumerate(narrativas):
        sim[r, i] = -9
        ordem = np.argsort(-sim[r])[:10]
        chave = next(iter(chaves[i]))
        rr.append(next((1 / (n + 1) for n, j in enumerate(ordem) if chave in chaves[j]), 0.0))
    metricas = {
        "decisoes_de_teste": len(teste),
        "infracoes_distintas": len(classes),
        "vizinhos_mesma_infracao_p5": round(p5, 3),
        "vizinhos_acaso_p5": round(acaso, 3),
        "acerto_infracao": round(acerto, 3),
        "busca_narrativa_mrr10": round(float(np.mean(rr)), 3),
        "consultas_narrativa": len(narrativas),
    }
    metricas_leg = avaliar_legislacao(votos, teste, leg[0], leg[1], leg[2], d, w_q, vocab_idx, idf) if leg else {}
    return metricas, metricas_leg


def exportar_legislacao(unidades, x_unid, citadas, d, w_q, dim, metricas: dict) -> None:
    """Grava os vetores da legislação e os artigos semelhantes já calculados."""
    u = vetores_legislacao(unidades, x_unid, _citantes(citadas), d, w_q)
    n = len(unidades)
    k = min(VIZINHOS, n - 1)
    vizinhos = np.zeros((n, k), dtype=np.uint16)
    sim = u @ u.T
    # Outra versão do mesmo item (outra aba da planilha) não conta como "semelhante".
    mesma = {}
    for j, unidade in enumerate(unidades):
        mesma.setdefault((unidade["doc"], unidade["numero"]), []).append(j)
    for grupo in mesma.values():
        sim[np.ix_(grupo, grupo)] = -9
    np.fill_diagonal(sim, -9)
    top = np.argpartition(-sim, k, axis=1)[:, :k]
    ordem = np.argsort(-np.take_along_axis(sim, top, axis=1), axis=1)
    vizinhos[:] = np.take_along_axis(top, ordem, axis=1)

    contagem = collections.Counter(chave for chaves in citadas for chave in chaves)
    np.round(u * 127).astype(np.int8).tofile(SAIDA / "legislacao_docs.bin")
    vizinhos.tofile(SAIDA / "legislacao_vizinhos.bin")
    modelo = {
        "versao": 1,
        "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "dim": dim,
        "n_docs": n,
        "n_vizinhos": k,
        "ids": [unidade["id"] for unidade in unidades],
        # Decisões do acervo cujos autos citam o artigo (0 para itens de tabela).
        "citacoes": [contagem.get(unidade["chave"], 0) if unidade["chave"] else 0 for unidade in unidades],
        "avaliacao": metricas,
    }
    destino = SAIDA / "legislacao.json"
    if destino.exists():
        anterior = json.loads(destino.read_text(encoding="utf-8"))
        if {**anterior, "gerado_em": ""} == {**modelo, "gerado_em": ""}:
            modelo["gerado_em"] = anterior["gerado_em"]
    destino.write_text(json.dumps(modelo, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"Legislação: {n} artigos/itens vigentes no espaço da IA", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dim", type=int, default=DIM)
    parser.add_argument("--epocas", type=int, default=EPOCAS)
    parser.add_argument("--sem-avaliacao", action="store_true", help="pula a medição em dados de teste")
    args = parser.parse_args()
    inicio = time.time()

    votos = carregar_votos()
    print(f"{len(votos)} decisões carregadas", flush=True)
    docs = [tokenizar(v.get("texto") or "") for v in votos]
    vocab, idf = montar_vocabulario(docs)
    vocab_idx = {t: i for i, t in enumerate(vocab)}
    x = tfidf(docs, vocab_idx, idf)
    print(f"{len(vocab)} termos no vocabulário", flush=True)

    # Gabarito: a infração do auto, registrada no SIF. Só decisões com um único auto têm rótulo inequívoco.
    chaves = [{chave_infracao(a.get("infracao")) for a in v.get("autos") or [] if a.get("infracao")} for v in votos]
    contagem = collections.Counter(k for ks in chaves for k in ks)
    nome_original = {}
    for v in votos:
        for a in v.get("autos") or []:
            if a.get("infracao"):
                nome_original.setdefault(chave_infracao(a["infracao"]), a["infracao"].strip())
    elegiveis = [
        i for i, v in enumerate(votos)
        if len(v.get("autos") or []) == 1 and chaves[i]
        and contagem[next(iter(chaves[i]))] >= MIN_DOCS_POR_INFRACAO
    ]

    unidades = carregar_legislacao()
    citadas = citacoes_por_decisao(votos)
    x_unid = tfidf([tokenizar(u["texto"]) for u in unidades], vocab_idx, idf) if unidades else None
    leg = (citadas, unidades, x_unid) if unidades else None

    if args.sem_avaliacao:
        # Mantém os números da última medição em vez de publicar o modelo sem eles.
        def anteriores(nome: str) -> dict:
            arquivo = SAIDA / nome
            return json.loads(arquivo.read_text(encoding="utf-8")).get("avaliacao", {}) if arquivo.exists() else {}

        metricas, metricas_leg = anteriores("modelo.json"), anteriores("legislacao.json")
    else:
        metricas, metricas_leg = avaliar(votos, x, chaves, elegiveis, vocab_idx, idf, args.dim, args.epocas, leg)
        print("Resultado em decisões nunca vistas:", json.dumps(metricas, ensure_ascii=False), flush=True)
        if metricas_leg:
            print("Legislação, a partir de decisões nunca vistas:", json.dumps(metricas_leg, ensure_ascii=False), flush=True)

    # Modelo final: mesmas configurações, agora com todas as decisões rotuladas.
    classes = sorted({next(iter(chaves[i])) for i in elegiveis})
    indice = {k: n for n, k in enumerate(classes)}
    rotulos = np.array([indice[next(iter(chaves[i]))] for i in elegiveis])
    print(f"Modelo final: {len(elegiveis)} decisões rotuladas, {len(classes)} infrações", flush=True)
    w, c, b = treinar(x[elegiveis], rotulos, len(classes), args.dim, args.epocas)
    w_q, escala = quantizar(w)
    d = vetores_de_documentos(x, w_q)

    vizinhos = np.zeros((len(votos), VIZINHOS), dtype=np.uint16)
    for ini in range(0, len(votos), 1024):
        sim = d[ini : ini + 1024] @ d.T
        sim[np.arange(sim.shape[0]), np.arange(ini, ini + sim.shape[0])] = -9
        top = np.argpartition(-sim, VIZINHOS, axis=1)[:, :VIZINHOS]
        ordem = np.argsort(-np.take_along_axis(sim, top, axis=1), axis=1)
        vizinhos[ini : ini + sim.shape[0]] = np.take_along_axis(top, ordem, axis=1)

    SAIDA.mkdir(parents=True, exist_ok=True)
    w_q.tofile(SAIDA / "termos.bin")
    np.round(d * 127).astype(np.int8).tofile(SAIDA / "docs.bin")
    vizinhos.tofile(SAIDA / "vizinhos.bin")
    np.array([resultado_da_decisao(v.get("texto") or "") for v in votos], dtype=np.uint8).tofile(SAIDA / "resultados.bin")
    np.concatenate([c.ravel(), b]).astype(np.float32).tofile(SAIDA / "classificador.bin")
    modelo = {
        "versao": 1,
        "gerado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "dim": args.dim,
        "n_docs": len(votos),
        "n_termos": len(vocab),
        "n_vizinhos": VIZINHOS,
        "escala_termos": escala,
        "resultados": RESULTADOS,
        "classes": [nome_original[k].upper() for k in classes],
        "avaliacao": metricas,
        "ids": [v["file_id"] for v in votos],
        "vocab": vocab,
        "idf": [round(float(valor), 4) for valor in idf],
    }
    # Acervo igual gera modelo igual: mantém a data anterior para não criar um commit à toa.
    destino = SAIDA / "modelo.json"
    if destino.exists():
        anterior = json.loads(destino.read_text(encoding="utf-8"))
        if {**anterior, "gerado_em": ""} == {**modelo, "gerado_em": ""}:
            modelo["gerado_em"] = anterior["gerado_em"]
    destino.write_text(json.dumps(modelo, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if unidades:
        exportar_legislacao(unidades, x_unid, citadas, d, w_q, args.dim, metricas_leg)
    tamanho = sum(f.stat().st_size for f in SAIDA.iterdir()) / 1e6
    print(f"Arquivos em {SAIDA.relative_to(RAIZ)}/ ({tamanho:.1f} MB) · {time.time() - inicio:.0f} s", flush=True)


if __name__ == "__main__":
    main()
