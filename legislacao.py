"""
Peças da legislação do site: leitura com redação revogada, divisão em artigos e
leitura das citações de dispositivos feitas nos autos do SIF.

Usado por preparar_pareceres.py --legislacao (extração e divisão) e por
treinar_ia.py (busca por significado e artigos semelhantes).

Os textos "-atual" da legislação municipal trazem a redação revogada riscada,
seguida da vigente. A extração em texto puro perde o risco, então aqui o
documento passa pelo HTML do LibreOffice, que o preserva (<strike>/<s>).
"""

from __future__ import annotations

import re
import unicodedata
from html.parser import HTMLParser

# --- Texto com trechos riscados ----------------------------------------------

_BLOCOS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "table", "br", "hr", "ul", "ol"}
_RISCO = {"s", "strike", "del"}
_IGNORAR = {"head", "style", "script", "title"}
_VAZIOS = {"br", "hr", "img", "meta", "link", "col", "input", "wbr", "area", "base", "param", "source"}
# Fora do plano básico o JavaScript conta 2 posições por caractere; sem eles (e sem
# BOM), as posições gravadas aqui valem igual no navegador.
_FORA_DO_BMP = re.compile(r"[\ufeff\U00010000-\U0010FFFF]")


class _Leitor(HTMLParser):
    """HTML do LibreOffice -> texto + marca de riscado por caractere.

    Parágrafos ficam separados por linha em branco; cada linha de tabela vira uma
    linha de texto, com as células não vazias separadas por " | ".
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chars: list[str] = []
        self.risco: list[bool] = []
        self.pilha: list[tuple[str, bool]] = []
        self.ignorar = 0
        self.celula = 0
        self.celula_com_texto = False
        self.linha_com_celula = False

    def _por(self, s: str, riscado: bool = False) -> None:
        self.chars.extend(s)
        self.risco.extend([riscado] * len(s))

    def _aparar(self) -> None:
        while self.chars and self.chars[-1] == " ":
            self.chars.pop()
            self.risco.pop()

    def _quebra(self, n: int) -> None:
        self._aparar()
        if not self.chars:
            return
        existentes = 0
        while existentes < len(self.chars) and self.chars[-1 - existentes] == "\n":
            existentes += 1
        if existentes < n:
            self._por("\n" * (n - existentes))

    def handle_starttag(self, tag, attrs):
        if tag in _IGNORAR:
            self.ignorar += 1
            return
        estilo = (dict(attrs).get("style") or "").replace(" ", "").lower()
        if tag in ("td", "th"):
            self.celula += 1
            self.celula_com_texto = False
        elif tag == "tr":
            self._quebra(1)
            self.linha_com_celula = False
        elif tag in _BLOCOS:
            if self.celula:
                if self.celula_com_texto and self.chars[-1] != " ":
                    self._por(" ")
            else:
                self._quebra(1 if tag in ("br", "li") else 2)
        if tag not in _VAZIOS:
            self.pilha.append((tag, tag in _RISCO or "line-through" in estilo))

    def handle_endtag(self, tag):
        if tag in _IGNORAR:
            self.ignorar = max(0, self.ignorar - 1)
            return
        for i in range(len(self.pilha) - 1, -1, -1):
            if self.pilha[i][0] == tag:
                del self.pilha[i:]
                break
        if tag in ("td", "th"):
            self.celula = max(0, self.celula - 1)
            self._aparar()
        elif tag == "tr":
            self._quebra(1)
        elif tag == "table":
            self._quebra(2)
        elif tag in _BLOCOS and not self.celula:
            self._quebra(1 if tag == "li" else 2)

    def handle_data(self, data):
        if self.ignorar:
            return
        s = re.sub(r"\s+", " ", _FORA_DO_BMP.sub("", data))
        if (self.celula and not self.celula_com_texto) or not self.chars or self.chars[-1] in "\n ":
            s = s.lstrip()
        if not s:
            return
        riscado = any(r for _, r in self.pilha)
        if self.celula and not self.celula_com_texto:
            if self.linha_com_celula:
                self._aparar()
                self._por(" | ", riscado)
            self.celula_com_texto = True
            self.linha_com_celula = True
        self._por(s, riscado)


def html_para_texto(html: str) -> tuple[str, list[list[int]]]:
    """Texto do documento e trechos riscados, como [início, fim) em posições do texto."""
    leitor = _Leitor()
    leitor.feed(html)
    leitor.close()
    leitor._aparar()
    texto = "".join(leitor.chars)
    riscado: list[list[int]] = []
    inicio = None
    for i, r in enumerate(leitor.risco + [False]):
        if r and inicio is None:
            inicio = i
        elif not r and inicio is not None:
            # Junta trechos separados só por espaço/quebra (parágrafos riscados seguidos).
            if riscado and not texto[riscado[-1][1] : inicio].strip():
                riscado[-1][1] = i
            else:
                riscado.append([inicio, i])
            inicio = None
    return texto, riscado


def sem_fora_do_bmp(texto: str) -> str:
    return _FORA_DO_BMP.sub("", texto)


# --- Normas e divisão em artigos ---------------------------------------------

# "lei10534_Anexo II...", "dec16529-anexo-I...", "lei-10534 -Anexo..." -> tipo + número.
_NOME_NORMA = re.compile(r"^(lei|dec)\W*(\d+)", re.IGNORECASE)
_NOME_ANEXO = re.compile(r"anexo[\s_\-]*([IVXL]+)\b", re.IGNORECASE)

# Cabeçalho de artigo: "Art. 10 - ", "Art. 1º – ", "Art. 45-A - ", "Art. 3º A - ".
# "Art. 17 revogado pelo..." também abre unidade (o artigo só existe como nota);
# "Art. 12 com redação dada..." e "Art. 16-A acrescentado..." são notas do anterior.
_ARTIGO = re.compile(
    r"^[ \t]*Art\.[ \t]*(\d+)[ºo°]?(?:[ \t]?-?[ \t]?([A-Z]))?(?:[ \t]*[-–][ \t]|[ \t]+(?=revogad))",
    re.M,
)
# Linha de tabela de penalidades: "96 | Utilizar mesas..." ou "3<tab>Invadir...".
_ITEM = re.compile(r"^[ \t]*(\d+[A-Z]?)[ \t]*(?:\||\t)", re.M)
# "Planilha 2: Tabela do Código" é a aba da planilha, como o LibreOffice a exporta.
_TITULO = re.compile(
    r"^[ \t]*(Planilha(?= \d+:)|T[ÍI]TULO|CAP[ÍI]TULO|Cap[íi]tulo|CAP\.|SE[ÇC][ÃA]O|Se[çc][ãa]o|SUBSE[ÇC][ÃA]O|Subse[çc][ãa]o)\b.*$",
    re.M,
)


def norma_do_nome(nome_arquivo: str) -> str | None:
    """'lei10534_Anexo II - Cap.VII.xls' -> 'Lei 10.534'. None se o nome não indica norma."""
    m = _NOME_NORMA.match(nome_arquivo.strip())
    if not m:
        return None
    tipo = "Lei" if m.group(1).lower() == "lei" else "Decreto"
    return f"{tipo} {int(m.group(2)):,}".replace(",", ".")


def chave_norma(tipo: str, numero: str | int) -> tuple[str, int]:
    """('Lei', '9.725') -> ('lei', 9725): a mesma chave para o acervo e para o SIF."""
    return ("lei" if tipo.strip().upper().startswith("LEI") else "decreto", int(str(numero).replace(".", "")))


# "Seção II com redação dada pelo Decreto..." é nota de alteração, não título.
_NOTA = re.compile(r"\b(com reda[çc][ãa]o|acrescentad|revogad|renumerad|vig[êe]ncia restaurada)", re.IGNORECASE)


def _titulos(texto: str) -> list[tuple[int, str, str]]:
    """[(posição, tipo, 'CAPÍTULO III – DO FECHAMENTO...')] na ordem do texto."""
    saida = []
    for m in _TITULO.finditer(texto):
        linha = m.group(0).strip()
        if _NOTA.search(linha):
            continue
        if len(linha) < 25 and m.group(1) != "Planilha":  # "CAPÍTULO III": o nome vem na linha seguinte
            resto = texto[m.end() : m.end() + 300].lstrip("\n").split("\n", 1)[0].strip()
            if resto and not _ARTIGO.match(resto) and not _TITULO.match(resto):
                linha = f"{linha} – {resto}"
        saida.append((m.start(), m.group(1).upper(), linha[:200]))
    return saida


def _contexto_em(titulos: list[tuple[int, str, str]], pos: int) -> str:
    # A aba só entra no contexto quando a planilha tem mais de uma.
    varias_abas = sum(tipo == "PLANILHA" for _, tipo, _ in titulos) > 1
    aba = capitulo = secao = ""
    for p, tipo, linha in titulos:
        if p > pos:
            break
        if tipo == "PLANILHA":
            aba, capitulo, secao = (linha if varias_abas else ""), "", ""
        elif tipo.startswith(("TÍTULO", "TITULO", "CAPÍTULO", "CAPITULO", "CAP.")):
            capitulo, secao = linha, ""
        else:
            secao = linha
    return " · ".join(x for x in (aba, capitulo, secao) if x)


def _fracao_riscada(riscado: list[list[int]], inicio: int, fim: int) -> float:
    if fim <= inicio:
        return 0.0
    total = sum(max(0, min(b, fim) - max(a, inicio)) for a, b in riscado)
    return total / (fim - inicio)


def dividir(texto: str, nome_arquivo: str, riscado: list[list[int]] | None = None) -> list[dict]:
    """Artigos (ou itens de tabela, nos anexos) do documento, com posições no texto.

    Cada unidade: id (único no documento), tipo, numero, rotulo, contexto
    (capítulo/seção), inicio, fim e revogado (cabeçalho riscado no original).
    """
    riscado = riscado or []
    anexo = _NOME_ANEXO.search(nome_arquivo)
    cortes = list((_ITEM if anexo else _ARTIGO).finditer(texto))
    titulos = _titulos(texto)
    brutas: list[dict] = []
    if not cortes:
        brutas.append({"tipo": "documento", "numero": "", "rotulo": "Documento inteiro", "inicio": 0, "fim": len(texto)})
    elif not anexo and texto[: cortes[0].start()].strip():
        brutas.append({"tipo": "preambulo", "numero": "", "rotulo": "Ementa e preâmbulo", "inicio": 0, "fim": cortes[0].start()})
    for k, m in enumerate(cortes):
        fim = cortes[k + 1].start() if k + 1 < len(cortes) else len(texto)
        if anexo:
            numero = m.group(1)
            unidade = {"tipo": "item", "numero": numero, "rotulo": f"Anexo {anexo.group(1).upper()}, item {numero}"}
        else:
            n = int(m.group(1))
            sufixo = f"-{m.group(2)}" if m.group(2) else ""
            unidade = {"tipo": "artigo", "numero": f"{n}{sufixo}", "rotulo": f"Art. {n}{'º' if n < 10 else ''}{sufixo}"}
        brutas.append({**unidade, "inicio": m.start(), "fim": fim})

    unidades = []
    vistos: dict[str, int] = {}
    for u in brutas:
        # Título de capítulo/seção no fim da unidade pertence à seguinte.
        seguinte = next((p for p, _, _ in titulos if u["inicio"] < p < u["fim"]), None)
        if seguinte is not None and u["tipo"] in ("artigo", "item", "preambulo"):
            u["fim"] = seguinte
        while u["fim"] > u["inicio"] and texto[u["fim"] - 1].isspace():
            u["fim"] -= 1
        cabecalho_fim = texto.find("\n", u["inicio"], u["fim"])
        cabecalho_fim = u["fim"] if cabecalho_fim < 0 else cabecalho_fim
        u["revogado"] = _fracao_riscada(riscado, u["inicio"], cabecalho_fim) >= 0.5
        base = {"artigo": "art-", "item": "item-"}.get(u["tipo"], u["tipo"]) + u["numero"]
        base += "-revogado" if u["revogado"] else ""
        vistos[base] = vistos.get(base, 0) + 1
        u["id"] = base if vistos[base] == 1 else f"{base}-{vistos[base]}"
        u["contexto"] = _contexto_em(titulos, u["inicio"]) if u["tipo"] in ("artigo", "item") else ""
        unidades.append(u)
    return unidades


# --- Citações nos autos do SIF -----------------------------------------------

_CIT_NORMA = re.compile(r"\b(LEI|DECRETO|DEC)\.?\s*(?:MUNICIPAL\s*)?(?:N\s*[O.]?\s*)?(\d{1,2}\.?\d{3})(?:\s*/\s*\d{2,4})?")
_CIT_PARAGRAFO = re.compile(r"§+\s*\d+\s*O?|PARAGRAFO\s+\d+\s*O?")
_CIT_INTERVALO = re.compile(r"(\d+)\s*O?\s+(?:A|ATE)\s+(\d+)")
_CIT_ARTIGO = re.compile(r"(\d+)(?:\s*O)?(?:\s*-\s*([A-Z])(?![A-Z])|([A-Z])(?![A-Z0-9]))?")


def _plano(s: str) -> str:
    s = unicodedata.normalize("NFD", s.upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.replace("º", "O").replace("°", "O")


def _expandir_intervalo(m: re.Match) -> str:
    a, b = int(m.group(1)), int(m.group(2))
    return ", ".join(str(n) for n in range(a, b + 1)) if 0 < b - a < 30 else m.group(0)


def citacoes(dispositivo: str) -> set[tuple[tuple[str, int], str]]:
    """Artigos citados no 'DISPOSITIVO LEGAL TRANSGREDIDO' de um auto.

    'LEI 9725/09 - ART. 31, DECRETO 13842/10, ARTS. 84, CAPUT E 89' ->
    {(('lei', 9725), '31'), (('decreto', 13842), '84'), (('decreto', 13842), '89')}
    Parágrafos e incisos são ignorados; intervalos ('ARTS. 41 A 44') são expandidos.
    """
    s = _plano(dispositivo or "")
    normas = list(_CIT_NORMA.finditer(s))
    saida = set()
    for k, m in enumerate(normas):
        trecho = s[m.end() : normas[k + 1].start() if k + 1 < len(normas) else len(s)]
        a = trecho.find("ART")
        if a < 0:
            continue
        trecho = _CIT_INTERVALO.sub(_expandir_intervalo, _CIT_PARAGRAFO.sub(" ", trecho[a:]))
        norma = chave_norma(m.group(1), m.group(2))
        for n in _CIT_ARTIGO.finditer(trecho):
            sufixo = n.group(2) or n.group(3)
            saida.add((norma, n.group(1) + (f"-{sufixo}" if sufixo else "")))
    return saida
