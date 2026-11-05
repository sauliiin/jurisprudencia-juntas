"""Perguntas sobre o Entendimento das Juntas, respondidas por um modelo de linguagem.

1. BM25 sobre os tópicos do Entendimento escolhe os que tratam da pergunta (milissegundos);
2. só esses tópicos vão para o modelo (endpoint no formato da OpenAI: Groq, Gemini...),
   com a instrução de responder apenas com base neles e citar [n];
3. as citações são conferidas aqui: número que não corresponde a um tópico enviado sai.

Sem chave, com o limite gratuito esgotado ou com o serviço fora do ar, a resposta vem sem
o texto do modelo, mas com os tópicos encontrados: quem pergunta nunca fica sem nada.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from api import __version__, entendimento
from api.config import Settings

log = logging.getLogger("api")

MAX_TOPICOS = 3
# Um tópico só acompanha o primeiro se tiver pelo menos esta fração da pontuação dele.
FRACAO_MINIMA = 0.6
NAO_TRATA = "O Entendimento das Juntas não trata deste tema."


class EntendimentoIndisponivel(RuntimeError):
    """Nem o JSON dos tópicos nem o acervo de pareceres estão disponíveis."""


class ModeloIndisponivel(RuntimeError):
    """O serviço de IA não respondeu (sem chave, limite esgotado, erro ou demora)."""


# ---------- Busca nos tópicos ----------

_TOKEN = re.compile(r"[a-z]{3,}")
_NAO_ASCII = re.compile(r"[^\x00-\x7f]")
# Palavras comuns demais para ajudar a achar o tópico (já sem acento, como saem de termos()).
_VAZIAS_TEXTO = """
    que para com uma umas uns por pelo pela pelos pelas nao sim dos das nos nas num numa como mais menos
    qual quais quem onde quando porque pois isso isto esse essa esses essas este esta estes estas aquele
    aquela ser sao foi era sera seja pode podem deve devem tem ter tendo sobre entre ate apos sem seu sua
    seus suas ele ela eles elas voce caso casos entao tambem muito ainda mesmo mesma outro outra
"""
_VAZIAS = frozenset(_VAZIAS_TEXTO.split())


def _raiz(token: str) -> str:
    # Plural simples do português: "notificações" = "notificação", "lotes" = "lote".
    if token.endswith("coes"):
        return token[:-4] + "cao"
    if token.endswith("oes"):
        return token[:-3] + "ao"
    if token.endswith("ais") and len(token) > 5:
        return token[:-3] + "al"
    if token.endswith("s") and len(token) > 4:
        return token[:-1]
    return token


def termos(texto: str) -> list[str]:
    """Minúsculas, sem acento, palavras de 3+ letras, sem as muito comuns, no singular."""
    plano = _NAO_ASCII.sub("", unicodedata.normalize("NFD", texto.lower()))
    return [_raiz(t) for t in _TOKEN.findall(plano) if t not in _VAZIAS]


@dataclass
class Topico:
    titulo: str
    texto: str


@dataclass
class Entendimento:
    arquivo: str
    link: str | None
    modificado_em: str | None
    topicos: list[Topico]
    _tf: list[Counter] = field(default_factory=list, repr=False)
    _tamanho: list[int] = field(default_factory=list, repr=False)
    _df: Counter = field(default_factory=Counter, repr=False)

    def __post_init__(self) -> None:
        for t in self.topicos:
            # O nome do próprio tópico conta duas vezes: "1.4 - Autuação para roçar lote penhorado"
            # diz do que ele trata.
            folha = t.titulo.split(" › ")[-1]
            tokens = termos(f"{folha} {t.titulo} {t.texto}")
            self._tf.append(Counter(tokens))
            self._tamanho.append(len(tokens))
            self._df.update(set(tokens))

    def pontuar(self, pergunta: str, k1: float = 1.5, b: float = 0.75) -> list[float]:
        """BM25 de cada tópico para a pergunta."""
        n = len(self.topicos)
        media = (sum(self._tamanho) / n) if n else 0
        consulta = set(termos(pergunta))
        notas = []
        for tf, tamanho in zip(self._tf, self._tamanho, strict=True):
            nota = 0.0
            for termo in consulta:
                f = tf.get(termo, 0)
                if not f:
                    continue
                df = self._df[termo]
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                nota += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * tamanho / media))
            notas.append(nota)
        return notas

    def buscar(self, pergunta: str, k: int = MAX_TOPICOS) -> list[Topico]:
        """Os tópicos que tratam da pergunta, do mais ao menos relevante (lista vazia se nenhum)."""
        notas = self.pontuar(pergunta)
        ordem = sorted(range(len(notas)), key=lambda i: -notas[i])
        if not ordem or notas[ordem[0]] <= 0:
            return []
        melhor = notas[ordem[0]]
        return [self.topicos[i] for i in ordem[:k] if notas[i] >= FRACAO_MINIMA * melhor]


def _de_dict(doc: dict) -> Entendimento:
    return Entendimento(
        arquivo=doc.get("arquivo") or entendimento.NOME_PADRAO,
        link=doc.get("link"),
        modificado_em=doc.get("modificado_em"),
        topicos=[Topico(t["titulo"], t["texto"]) for t in doc.get("topicos", [])],
    )


@lru_cache(maxsize=4)
def _carregar(json_path: Path) -> Entendimento:
    if not json_path.is_file():
        raise EntendimentoIndisponivel(f"Entendimento não encontrado em {json_path}")
    return _de_dict(json.loads(json_path.read_text(encoding="utf-8")))


def carregar(settings: Settings) -> Entendimento:
    return _carregar(settings.entendimento or settings.dados_dir / "entendimento.json")


# ---------- O que vai para o modelo ----------


def recorte(texto: str, pergunta: str, limite: int) -> str:
    """O tópico inteiro, se couber; senão, os parágrafos com mais termos da pergunta, na ordem original."""
    if len(texto) <= limite:
        return texto
    paragrafos = [p for p in texto.split("\n\n") if p.strip()]
    consulta = set(termos(pergunta))
    nota = [len(consulta & set(termos(p))) for p in paragrafos]
    escolhidos: set[int] = set()
    usado = 0
    for i in sorted(range(len(paragrafos)), key=lambda i: (-nota[i], i)):
        if usado + len(paragrafos[i]) > limite:
            continue
        escolhidos.add(i)
        usado += len(paragrafos[i]) + 2
    if not escolhidos:
        return paragrafos[0][:limite].rsplit(" ", 1)[0] + " […]"
    partes, anterior = [], -1
    for i in sorted(escolhidos):
        if i != anterior + 1:
            partes.append("[…]")
        partes.append(paragrafos[i])
        anterior = i
    if anterior != len(paragrafos) - 1:
        partes.append("[…]")
    return "\n\n".join(partes)


INSTRUCOES = f"""Você responde perguntas sobre o ENTENDIMENTO DAS JUNTAS: o Vade Mecum de jurisprudência \
administrativa das Juntas Integradas de Julgamento Fiscal de Belo Horizonte (2024).

Regras:
- Use apenas os trechos numerados enviados. Não use conhecimento de fora, não invente artigos de lei, \
prazos, valores nem decisões.
- Cite o trecho usado logo depois da frase, assim: [1] ou [2].
- Se os trechos não respondem à pergunta, escreva só: "{NAO_TRATA}"
- Responda em português claro, em no máximo três parágrafos curtos, em texto simples (sem títulos, \
sem listas, sem markdown)."""


def mensagens(pergunta: str, topicos: list[Topico], max_chars: int) -> list[dict]:
    limite = max_chars // max(len(topicos), 1)
    trechos = "\n\n".join(
        f"[{n}] {t.titulo}\n{recorte(t.texto, pergunta, limite)}" for n, t in enumerate(topicos, start=1)
    )
    return [
        {"role": "system", "content": INSTRUCOES},
        {"role": "user", "content": f"Trechos do Entendimento das Juntas:\n\n{trechos}\n\nPergunta: {pergunta}"},
    ]


_CITACAO = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")


def conferir_citacoes(texto: str, n_fontes: int) -> tuple[str, int]:
    """Texto só com as citações [n] que existem e quantas foram mantidas."""
    mantidas = 0

    def trocar(m: re.Match) -> str:
        nonlocal mantidas
        validas = [n for n in re.split(r"\s*[,;]\s*", m.group(1)) if 1 <= int(n) <= n_fontes]
        mantidas += len(validas)
        return "".join(f"[{n}]" for n in validas)

    limpo = _CITACAO.sub(trocar, texto)
    return re.sub(r"[ \t]+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", limpo)).strip(), mantidas


# ---------- Chamada ao modelo ----------

_chave_cache: dict[str, str] = {}


def chave_da_api(settings: Settings) -> str | None:
    """A chave do serviço: variável de ambiente (local) ou parâmetro SecureString do SSM (Lambda)."""
    if settings.llm_api_key:
        return settings.llm_api_key.get_secret_value() or None
    nome = settings.llm_api_key_ssm
    if not nome:
        return None
    if nome not in _chave_cache:
        try:
            import boto3  # já vem no runtime Python do Lambda

            resposta = boto3.client("ssm").get_parameter(Name=nome, WithDecryption=True)
            _chave_cache[nome] = resposta["Parameter"]["Value"]
        except Exception as exc:  # parâmetro inexistente, sem permissão, sem rede
            log.warning("chave_llm_indisponivel", extra={"detalhe": type(exc).__name__})
            return None
    return _chave_cache[nome]


def chamar_modelo(msgs: list[dict], settings: Settings, chave: str) -> str:
    pedido = {"model": settings.llm_modelo, "messages": msgs, "temperature": 0.1, "max_tokens": 600}
    corpo = json.dumps(pedido).encode("utf-8")
    req = urllib.request.Request(
        settings.llm_url,
        data=corpo,
        headers={
            "Authorization": f"Bearer {chave}",
            "Content-Type": "application/json",
            # Alguns provedores recusam o User-Agent padrão do urllib.
            "User-Agent": f"omniai-jurisprudencia/{__version__}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.llm_timeout) as resp:
            dados = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ModeloIndisponivel(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ModeloIndisponivel(type(exc).__name__) from exc
    try:
        texto = dados["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModeloIndisponivel("resposta sem texto") from exc
    if not isinstance(texto, str) or not texto.strip():
        raise ModeloIndisponivel("resposta vazia")
    # Modelos que "pensam" em voz alta (<think>...</think>) não devem mostrar isso.
    return re.sub(r"<think>.*?</think>", "", texto, flags=re.S).strip()


# ---------- Resposta ----------

AVISO_SEM_TOPICO = (
    "Nenhum tópico do Entendimento das Juntas trata destes termos. Tente outras palavras ou busque nas decisões."
)
AVISO_SEM_MODELO = (
    "A resposta escrita pela IA não está disponível agora. Abaixo, os tópicos do Entendimento que tratam da pergunta."
)
AVISO_NAO_TRATA = "O Entendimento não responde diretamente a esta pergunta. Confira os tópicos mais próximos abaixo."
AVISO_SEM_CITACAO = "A resposta não citou os tópicos consultados: confira o texto deles abaixo."


def responder(pergunta: str, settings: Settings) -> dict:
    inicio = time.perf_counter()
    doc = carregar(settings)
    topicos = doc.buscar(pergunta)
    saida: dict = {
        "pergunta": pergunta,
        "resposta": None,
        "aviso": None,
        "modelo": None,
        "documento": {"arquivo": doc.arquivo, "link": doc.link, "modificado_em": doc.modificado_em},
        "fontes": [{"n": n, "titulo": t.titulo, "texto": t.texto} for n, t in enumerate(topicos, start=1)],
    }
    if not topicos:
        saida["aviso"] = AVISO_SEM_TOPICO
        return saida

    chave = chave_da_api(settings)
    motivo = "sem_chave"
    if chave:
        try:
            bruto = chamar_modelo(mensagens(pergunta, topicos, settings.llm_max_chars), settings, chave)
            texto, citacoes = conferir_citacoes(bruto, len(topicos))
            saida["resposta"], saida["modelo"] = texto, settings.llm_modelo
            if NAO_TRATA.lower().rstrip(".") in texto.lower():
                saida["aviso"] = AVISO_NAO_TRATA
            elif citacoes == 0:
                saida["aviso"] = AVISO_SEM_CITACAO
            motivo = "ok"
        except ModeloIndisponivel as exc:
            motivo = str(exc)
    if saida["resposta"] is None:
        saida["aviso"] = AVISO_SEM_MODELO
    # A pergunta não vai para o log: pode trazer dados pessoais.
    log.info(
        "pergunta",
        extra={
            "caracteres": len(pergunta),
            "topicos": len(topicos),
            "modelo": motivo,
            "duracao_ms": round((time.perf_counter() - inicio) * 1000, 1),
        },
    )
    return saida
