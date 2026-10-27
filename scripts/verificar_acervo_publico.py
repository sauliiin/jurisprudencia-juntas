#!/usr/bin/env python3
"""
Confere se o acervo publicado em site_data/ não expõe dados que devem ficar locais.

    python3 scripts/verificar_acervo_publico.py

Falha (código 1) se:
  - algum arquivo de site_data/ contém "caminho_bruto" ou um caminho começando em /home/
    (URLs como https://alf.pbh.gov.br/publico/home/..., citadas nos votos, não contam);
  - algum auto de site_data/votos.jsonl ainda tem o campo autuado;
  - o nome de algum autuado (lido do indice_busca.db, que guarda os dados completos)
    aparece em qualquer registro de votos.jsonl. Com --so-propria-decisao, só conta o
    nome na decisão em que ele é autuado (é o que preparar_acervo_publico.py mascara).

O nome é procurado palavra por palavra, sem diferenciar caixa, acentos, espaços e
pontuação, e em todos os registros, não só no da decisão do próprio autuado. Nomes com
menos de 5 caracteres ficam de fora, como no mascaramento de preparar_acervo_publico.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PROIBIDOS = {
    "caminho_bruto": re.compile(rb"caminho_bruto"),
    "/home/": re.compile(rb"(?<![\w.~-])/home/"),
}
AUTUADO_MIN_CHARS = 5


def _palavras(texto: str) -> list[str]:
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch)).casefold()
    return re.findall(r"\w+", texto)


def _strings(valor) -> list[str]:
    if isinstance(valor, str):
        return [valor]
    if isinstance(valor, dict):
        return [s for v in valor.values() for s in _strings(v)]
    if isinstance(valor, list):
        return [s for v in valor for s in _strings(v)]
    return []


def _autuados(indice: Path) -> dict[str, set[str]]:
    """Nome do autuado -> file_ids das decisões em que ele é autuado."""
    con = sqlite3.connect(f"file:{indice}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT voto_file_id, autuado FROM autos").fetchall()
    finally:
        con.close()
    nomes: dict[str, set[str]] = defaultdict(set)
    for file_id, autuado in rows:
        autuado = " ".join((autuado or "").split())
        if len(autuado) >= AUTUADO_MIN_CHARS:
            nomes[autuado].add(file_id)
    return nomes


def verificar_proibidos(site_data: Path) -> list[str]:
    falhas = []
    for arquivo in sorted(p for p in site_data.rglob("*") if p.is_file()):
        with arquivo.open("rb") as fh:
            for numero, linha in enumerate(fh, start=1):
                for termo, padrao in PROIBIDOS.items():
                    if padrao.search(linha):
                        falhas.append(f"{arquivo.relative_to(RAIZ)}:{numero} contém {termo!r}")
    return falhas


def verificar_autuados(votos: Path, nomes: dict[str, set[str]]) -> tuple[list[str], list[str]]:
    por_palavras: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for nome in nomes:
        chave = tuple(_palavras(nome))
        if chave:
            por_palavras[chave].append(nome)
    tamanhos: dict[str, set[int]] = defaultdict(set)
    for chave in por_palavras:
        tamanhos[chave[0]].add(len(chave))

    campo_autuado = []
    achados = []
    with votos.open(encoding="utf-8") as fh:
        for numero, linha in enumerate(fh, start=1):
            if not linha.strip():
                continue
            record = json.loads(linha)
            if any("autuado" in auto for auto in record.get("autos", [])):
                campo_autuado.append(f"linha {numero}: auto ainda tem o campo autuado")
            # Cada campo separado, para um nome não "casar" atravessando dois campos.
            for texto in _strings(record):
                palavras = _palavras(texto)
                for i, palavra in enumerate(palavras):
                    for tamanho in tamanhos.get(palavra, ()):
                        for nome in por_palavras.get(tuple(palavras[i : i + tamanho]), ()):
                            achados.append((numero, record.get("file_id", ""), nome))
    return campo_autuado, achados


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site-data", default=str(RAIZ / "site_data"))
    parser.add_argument("--indice", default=str(RAIZ / "indice_busca.db"))
    parser.add_argument("--exemplos", type=int, default=15, help="quantos achados listar")
    parser.add_argument(
        "--so-propria-decisao",
        action="store_true",
        help="não falha por autuado citado em outra decisão, só na própria",
    )
    args = parser.parse_args()

    site_data = Path(args.site_data)
    votos = site_data / "votos.jsonl"
    nomes = _autuados(Path(args.indice))

    proibidos = verificar_proibidos(site_data)
    campo_autuado, achados = verificar_autuados(votos, nomes)
    proprios = [a for a in achados if a[1] in nomes[a[2]]]
    outros = [a for a in achados if a[1] not in nomes[a[2]]]

    print(f"Arquivos em site_data/ com caminho_bruto ou /home/ : {len(proibidos)} ocorrências")
    for falha in proibidos[: args.exemplos]:
        print(f"  {falha}")
    print(f"Autos com o campo autuado                         : {len(campo_autuado)}")
    print(f"Autuados conferidos (do indice_busca.db)          : {len(nomes)}")
    print(f"Autuado na própria decisão                        : {len(proprios)} ocorrências")
    print(
        f"Autuado em outra decisão                          : {len(outros)} ocorrências, "
        f"{len({a[2] for a in outros})} nomes, {len({a[1] for a in outros})} decisões"
    )
    for numero, file_id, nome in (proprios + outros)[: args.exemplos]:
        print(f"  linha {numero} ({file_id}): {nome}")

    ok = not (proibidos or campo_autuado or proprios or (outros and not args.so_propria_decisao))
    print("OK" if ok else "FALHOU")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
