"""Entendimento das Juntas (Vade Mecum de jurisprudência administrativa) dividido por tópico.

O documento é um .docx compartilhado no Google Drive. Este módulo o baixa pelo ID, divide pelos
tópicos do sumário e grava um JSON pequeno, versionado em site_data/ e levado ao pacote do Lambda:

    python -m api.entendimento                        # baixa do Drive -> site_data/entendimento.json
    python -m api.entendimento --docx ENTENDIMENTO.docx   # lê um arquivo local

Editar o mesmo arquivo no Drive basta: a próxima atualização (atualizar_tudo.sh) baixa de novo.
O parser vem do assistente de jurisprudência offline (assistente/normas.py), com o sumário
reconhecido também pelo número de página solto e títulos que começam com aspas.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from email.utils import parsedate_to_datetime
from pathlib import Path

from api import __version__

# "ENTENDIMENTO JUNTAS 2026 - versão final.docx", compartilhado para qualquer pessoa com o link.
DRIVE_ID = "1HPKaJqWxYVkk2dZy9_SSDUtfm_zMCuI-"
# "VADE MECUM - edição em livro.pdf": a versão para leitura, aberta pelo link "Entendimento completo".
PDF_ID = "1hV3234g0YfCiZEGCujkgvzwr1GcH2BKN"
NOME_PADRAO = "Entendimento das Juntas"
RAIZ = Path(__file__).resolve().parent.parent
_DOWNLOAD = "https://drive.google.com/uc?export=download&id={}"
_VER = "https://drive.google.com/file/d/{}/view"

_CAPITULO = re.compile(r"^C[ÁA]P[ÍI]TULO\s+([IVX]+)\s*[-–]?\s*(.*)$", re.I)
# "1.1 - Casa de shows", "2.1 Área pública...", "1.5– Notificação...", "3.2 – “Falta” do laudo".
# Incisos (I -) e "1)" não são tópicos.
_TOPICO = re.compile(r"^(\d+(?:\.\d+)*)\s*[-–]?\s*(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ“\"‘«])")
# Linha do sumário: termina no número da página ("Casa de shows 5" ou "Casa de shows - Pg. 5").
_SUMARIO = re.compile(r"[-–]\s*Pg\.?\s*\d+\s*$", re.I)
_PAGINA = re.compile(r"(?:\s\d{1,3}|Pg\.?\s*\d+)\s*$", re.I)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_ESPACOS = {_W + "tab", _W + "br", _W + "cr"}
_MODIFICADO = "{http://purl.org/dc/terms/}modified"


def paragrafos(texto: str) -> list[str]:
    """Parágrafos separados por linha em branco, com os espaços normalizados."""
    blocos = (re.sub(r"\s+", " ", bloco).strip() for bloco in re.split(r"\n\s*\n", texto))
    return [p for p in blocos if p]


def topicos(paragrafos: list[str]) -> list[dict]:
    """[{"titulo": "CAPÍTULO VI – TEMAS DE DIREITO › 1 - Dupla visita. › 1.1 - Mesas...", "texto": ...}].

    O sumário é pulado (do título "SUMÁRIO" até a primeira linha sem número de página, e as
    linhas terminadas em "Pg. N"); a apresentação antes do primeiro capítulo vira um tópico
    próprio. O título leva os tópicos-pai, para que "1.1 - Mesas e cadeiras" seja achado
    também por "dupla visita".
    """
    saida: list[dict] = []
    capitulo = "Apresentação"
    pais: dict[str, str] = {}
    atual: dict = {"titulo": capitulo, "texto": []}
    no_sumario = False

    def fechar() -> None:
        if atual["texto"]:
            saida.append({"titulo": atual["titulo"], "texto": "\n\n".join(atual["texto"])})

    for p in paragrafos:
        if p.upper() == "SUMÁRIO":
            no_sumario = True
            continue
        if no_sumario and _PAGINA.search(p):
            continue
        no_sumario = False
        if _SUMARIO.search(p):
            continue
        cap = _CAPITULO.match(p)
        topico = _TOPICO.match(p)
        if cap:
            fechar()
            capitulo = f"CAPÍTULO {cap.group(1).upper()} – {cap.group(2).strip().upper()}".rstrip(" –")
            pais = {}
            atual = {"titulo": capitulo, "texto": []}
        elif topico:
            fechar()
            numero = topico.group(1)
            resto = p[topico.end() :]
            # Título colado ao texto ("1- Alvará ... - O Alvará de Localização é..."): corta no traço seguinte.
            titulo, corpo = resto, ""
            if len(resto) > 200:
                m = re.search(r"\s[-–]\s", resto)
                titulo, corpo = (resto[: m.start()], resto[m.end() :]) if m else (resto[:120], resto)
            pais[numero] = f"{numero} - {titulo.strip()}"
            caminho_pais = [pais[n] for n in pais if numero.startswith(n + ".")]
            atual = {"titulo": " › ".join([capitulo, *caminho_pais, pais[numero]]), "texto": [corpo] if corpo else []}
        else:
            atual["texto"].append(p)
    fechar()
    return saida


# ---------- Leitura do .docx ----------


def _texto_paragrafo(paragrafo: ET.Element) -> str:
    partes: list[str] = []

    def visitar(elemento: ET.Element) -> None:
        for filho in elemento:
            if filho.tag == _W + "p":  # parágrafo de caixa de texto: é lido à parte
                continue
            if filho.tag == _W + "t":
                partes.append(filho.text or "")
            elif filho.tag in _ESPACOS:
                partes.append(" ")
            visitar(filho)

    visitar(paragrafo)
    return "".join(partes)


def ler_docx(conteudo: bytes) -> tuple[str, str | None]:
    """(texto com um parágrafo por bloco, data da última modificação gravada no arquivo)."""
    try:
        with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
            documento = ET.fromstring(z.read("word/document.xml"))
            core = ET.fromstring(z.read("docProps/core.xml")) if "docProps/core.xml" in z.namelist() else None
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"não é um .docx legível ({exc})") from exc
    texto = "\n\n".join(_texto_paragrafo(p) for p in documento.iter(_W + "p"))
    modificado = core.findtext(_MODIFICADO) if core is not None else None
    return texto, (modificado or None)


def _nome_do_cabecalho(disposicao: str) -> str | None:
    """Nome do arquivo no Content-Disposition (o Drive manda UTF-8 cru, que o urllib lê como latin-1)."""
    if m := re.search(r"filename\*=UTF-8''([^;]+)", disposicao, re.I):
        return urllib.parse.unquote(m.group(1)).strip()
    if m := re.search(r'filename="([^"]+)"', disposicao):
        nome = m.group(1)
        try:
            return nome.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return nome
    return None


def _data_http(valor: str | None) -> str | None:
    try:
        return parsedate_to_datetime(valor).strftime("%Y-%m-%dT%H:%M:%SZ") if valor else None
    except (TypeError, ValueError):
        return None


def baixar_do_drive(file_id: str, timeout: float = 60) -> tuple[str, bytes, str | None]:
    """(nome do arquivo, conteúdo, última modificação no Drive) de um arquivo compartilhado por link."""
    pedido = urllib.request.Request(
        _DOWNLOAD.format(urllib.parse.quote(file_id)), headers={"User-Agent": f"omniai-jurisprudencia/{__version__}"}
    )
    with urllib.request.urlopen(pedido, timeout=timeout) as resposta:
        conteudo = resposta.read()
        nome = _nome_do_cabecalho(resposta.headers.get("Content-Disposition", ""))
        modificado = _data_http(resposta.headers.get("Last-Modified"))
    if not conteudo.startswith(b"PK"):
        raise ValueError("o Drive não devolveu um .docx (está compartilhado para qualquer pessoa com o link?)")
    return nome or f"{file_id}.docx", conteudo, modificado


def documento(conteudo: bytes, arquivo: str, link: str | None = None, modificado: str | None = None) -> dict:
    """{"arquivo", "link", "modificado_em", "topicos"} de um .docx do Entendimento."""
    texto, gravado = ler_docx(conteudo)
    return {
        "arquivo": arquivo,
        "link": link,
        "modificado_em": modificado or gravado,
        "topicos": topicos(paragrafos(texto)),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Divide o Entendimento das Juntas por tópico.")
    parser.add_argument("--drive-id", default=DRIVE_ID, help="ID do .docx no Google Drive (compartilhado por link)")
    parser.add_argument("--docx", type=Path, help="lê este .docx local em vez de baixar do Drive")
    parser.add_argument("--pdf-id", default=PDF_ID, help="ID do PDF no Drive, aberto pelo link do documento")
    parser.add_argument("--saida", type=Path, default=RAIZ / "site_data" / "entendimento.json")
    args = parser.parse_args(argv)

    try:
        if args.docx:
            doc = documento(args.docx.read_bytes(), args.docx.name, _VER.format(args.pdf_id))
        else:
            nome, conteudo, modificado = baixar_do_drive(args.drive_id)
            doc = documento(conteudo, nome, _VER.format(args.pdf_id), modificado)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Entendimento não lido: {exc}") from exc
    # Só a apresentação (ou nada): não é o Entendimento, ou o formato dos títulos mudou.
    if all(t["titulo"] == "Apresentação" for t in doc["topicos"]):
        raise SystemExit(f"Entendimento não lido: nenhum tópico em {doc['arquivo']}")
    args.saida.parent.mkdir(parents=True, exist_ok=True)
    args.saida.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(doc['topicos'])} tópicos de {doc['arquivo']} -> {args.saida}")


if __name__ == "__main__":
    main()
