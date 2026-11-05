"""Entendimento das Juntas (Vade Mecum de jurisprudência administrativa) dividido por tópico.

O documento está no acervo de pareceres (site_data/pareceres.jsonl), com o texto já extraído
do .docx. Este módulo o divide pelos tópicos do sumário e grava um JSON pequeno, que vai no
pacote do Lambda junto com o índice:

    python -m api.entendimento                      # -> entendimento.json
    python -m api.entendimento --saida build/lambda/entendimento.json

O parser é o mesmo do assistente de jurisprudência offline (assistente/normas.py).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ARQUIVO = "ENTENDIMENTO JUNTAS 2024.docx"
RAIZ = Path(__file__).resolve().parent.parent

_CAPITULO = re.compile(r"^C[ÁA]P[ÍI]TULO\s+([IVX]+)\s*[-–]?\s*(.*)$", re.I)
# "1.1 - Casa de shows", "2.1 Área pública...", "1.5– Notificação...". Incisos (I -) e "1)" não são tópicos.
_TOPICO = re.compile(r"^(\d+(?:\.\d+)*)\s*[-–]?\s*(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ])")
_SUMARIO = re.compile(r"[-–]\s*Pg\.?\s*\d+\s*$", re.I)


def paragrafos(texto: str) -> list[str]:
    """Texto extraído pelo pipeline: parágrafos separados por linha em branco."""
    blocos = (re.sub(r"\s+", " ", bloco).strip() for bloco in re.split(r"\n\s*\n", texto))
    return [p for p in blocos if p]


def topicos(paragrafos: list[str]) -> list[dict]:
    """[{"titulo": "CAPÍTULO VI – TEMAS DE DIREITO › 1 - Dupla visita. › 1.1 - Mesas...", "texto": ...}].

    O sumário (linhas terminadas em "Pg. N") é pulado; a apresentação antes do primeiro
    capítulo vira um tópico próprio. O título leva os tópicos-pai, para que "1.1 - Mesas e
    cadeiras" seja achado também por "dupla visita".
    """
    saida: list[dict] = []
    capitulo = "Apresentação"
    pais: dict[str, str] = {}
    atual: dict = {"titulo": capitulo, "texto": []}

    def fechar() -> None:
        if atual["texto"]:
            saida.append({"titulo": atual["titulo"], "texto": "\n\n".join(atual["texto"])})

    for p in paragrafos:
        if _SUMARIO.search(p) or p.upper() == "SUMÁRIO":
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


def do_acervo(pareceres_jsonl: Path, arquivo: str = ARQUIVO) -> dict | None:
    """{"arquivo", "link", "modificado_em", "topicos"} do documento no acervo, ou None se não estiver lá."""
    with pareceres_jsonl.open(encoding="utf-8") as fh:
        for linha in fh:
            if arquivo not in linha:
                continue
            doc = json.loads(linha)
            if doc.get("nome_arquivo") != arquivo:
                continue
            return {
                "arquivo": arquivo,
                "link": doc.get("drive_view_url") or None,
                "modificado_em": doc.get("modificado_em") or None,
                "topicos": topicos(paragrafos(doc.get("texto") or "")),
            }
    return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Divide o Entendimento das Juntas por tópico.")
    parser.add_argument("--pareceres", type=Path, default=RAIZ / "site_data" / "pareceres.jsonl")
    parser.add_argument("--saida", type=Path, default=RAIZ / "entendimento.json")
    parser.add_argument("--arquivo", default=ARQUIVO, help="nome_arquivo do documento no acervo")
    args = parser.parse_args(argv)

    doc = do_acervo(args.pareceres, args.arquivo)
    if doc is None or not doc["topicos"]:
        raise SystemExit(f"{args.arquivo} não encontrado (ou sem tópicos) em {args.pareceres}")
    args.saida.parent.mkdir(parents=True, exist_ok=True)
    args.saida.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    print(f"{len(doc['topicos'])} tópicos -> {args.saida}")


if __name__ == "__main__":
    main()
