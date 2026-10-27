"""Gera o índice SQLite público que a API lê, a partir do acervo já mascarado.

A API nunca abre o indice_busca.db do pipeline: ele guarda o autuado, o texto sem
máscara e caminhos desta máquina. Este índice é montado de site_data/votos.jsonl (o
mesmo arquivo publicado no site), com as mesmas tabelas e a mesma busca FTS5.

    python -m api.indice
    python -m api.indice --jsonl site_data/votos.jsonl --saida indice_publico.db

Opcionais: --ia (resultado de cada decisão lido pelo modelo do site), --marco (contagem
de documentos pulados) e --arquivos-de (caminho relativo dos originais, para /api/file).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

SCHEMA = """
CREATE TABLE votos (
    file_id               TEXT PRIMARY KEY,
    nome_arquivo          TEXT NOT NULL,
    instancia             TEXT NOT NULL,
    decisao_instancia     TEXT NOT NULL,
    protocolo             TEXT,
    assunto               TEXT,
    texto                 TEXT,
    data                  TEXT,
    resultado             TEXT,
    drive_file_id_publico TEXT,
    arquivo               TEXT
);
CREATE INDEX votos_data ON votos (data DESC);
CREATE TABLE autos (
    voto_file_id                    TEXT NOT NULL,
    numero                          TEXT NOT NULL,
    infracao                        TEXT,
    dispositivo_legal_transgredido  TEXT,
    local_constatacao               TEXT,
    lei                             TEXT,
    pdf_encontrado                  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (voto_file_id, numero)
);
CREATE TABLE meta (chave TEXT PRIMARY KEY, valor TEXT);
CREATE VIRTUAL TABLE votos_fts USING fts5(
    file_id UNINDEXED,
    nome_arquivo,
    decisao_instancia,
    protocolo,
    assunto,
    texto,
    autos_texto,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""

# A data da decisão é a última data por extenso do texto (a da assinatura); o site usa a
# mesma regra quando roda sem a API.
_MESES = [
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
]
_DATA = re.compile(
    r"\b(\d{1,2})º?\s+de\s+(janeiro|fevereiro|março|marco|abril|maio|junho|julho|agosto|setembro"
    r"|outubro|novembro|dezembro)\s+de\s+(20\d{2})\b",
    re.IGNORECASE,
)


def data_da_decisao(texto: str) -> str | None:
    """Data ISO (AAAA-MM-DD) da última data por extenso do texto, ou None."""
    achados = list(_DATA.finditer(texto or ""))
    if not achados:
        return None
    dia, mes, ano = achados[-1].groups()
    numero_mes = _MESES.index(mes.lower().replace("marco", "março")) + 1
    if not 1 <= int(dia) <= 31:
        return None
    return f"{ano}-{numero_mes:02d}-{int(dia):02d}"


def resultados_da_ia(pasta_ia: Path) -> dict[str, str]:
    """file_id -> resultado ("deferido", "indeferido"...) publicado pelo treino em site_data/ia/."""
    modelo_path = pasta_ia / "modelo.json"
    binario_path = pasta_ia / "resultados.bin"
    if not modelo_path.exists() or not binario_path.exists():
        return {}
    modelo = json.loads(modelo_path.read_text(encoding="utf-8"))
    codigos = binario_path.read_bytes()
    rotulos = modelo.get("resultados", [])
    return {
        file_id: rotulos[codigo]
        for file_id, codigo in zip(modelo.get("ids", []), codigos, strict=False)
        if codigo < len(rotulos) and rotulos[codigo]
    }


def pulados_do_marco(marco: Path) -> int:
    if not marco.exists():
        return 0
    contagens = json.loads(marco.read_text(encoding="utf-8")).get("counts", {})
    return int(contagens.get("pulados", 0) or 0)


def arquivos_do_indice_privado(indice: Path, raiz: str = "votos_brutos") -> dict[str, str]:
    """file_id -> caminho relativo do original em votos_brutos/ (só o trecho depois da raiz)."""
    con = sqlite3.connect(f"file:{indice}?mode=ro", uri=True)
    try:
        linhas = con.execute("SELECT file_id, caminho_bruto FROM votos").fetchall()
    finally:
        con.close()
    relativos = {}
    for file_id, caminho in linhas:
        partes = Path(caminho or "").parts
        if raiz in partes:
            relativos[file_id] = str(Path(*partes[partes.index(raiz) + 1 :]))
    return relativos


def ler_jsonl(caminho: Path) -> Iterator[dict]:
    with caminho.open(encoding="utf-8") as fh:
        for linha in fh:
            if linha.strip():
                yield json.loads(linha)


def _autos_texto(autos: Iterable[dict]) -> str:
    campos = ("numero", "infracao", "dispositivo_legal_transgredido", "local_constatacao", "lei")
    return "\n".join(" ".join(str(auto[c]) for c in campos if auto.get(c)) for auto in autos)


def gerar_indice(
    registros: Iterable[dict],
    saida: Path,
    *,
    resultados: dict[str, str] | None = None,
    arquivos: dict[str, str] | None = None,
    pulados: int = 0,
    origem: str = "",
) -> int:
    """Grava o índice em `saida` (substitui o anterior só no fim) e devolve o nº de decisões."""
    resultados = resultados or {}
    arquivos = arquivos or {}
    saida.parent.mkdir(parents=True, exist_ok=True)
    temporario = saida.with_name(saida.name + ".tmp")
    temporario.unlink(missing_ok=True)
    con = sqlite3.connect(temporario)
    try:
        con.executescript(SCHEMA)
        total = 0
        for registro in registros:
            file_id = str(registro["file_id"])
            autos = [auto for auto in registro.get("autos") or [] if isinstance(auto, dict)]
            # O autuado não entra no índice público, mesmo que venha num JSONL antigo.
            for auto in autos:
                auto.pop("autuado", None)
            con.execute(
                "INSERT INTO votos VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    registro.get("nome_arquivo") or file_id,
                    registro.get("instancia") or "",
                    registro.get("decisao_instancia") or "Decisão",
                    registro.get("protocolo") or "",
                    registro.get("assunto") or "",
                    registro.get("texto") or "",
                    data_da_decisao(registro.get("texto") or ""),
                    resultados.get(file_id),
                    registro.get("drive_file_id_publico"),
                    arquivos.get(file_id),
                ),
            )
            con.executemany(
                "INSERT OR IGNORE INTO autos VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        file_id,
                        str(auto.get("numero") or ""),
                        auto.get("infracao") or "",
                        auto.get("dispositivo_legal_transgredido") or "",
                        auto.get("local_constatacao") or "",
                        auto.get("lei") or "",
                        int(bool(auto.get("pdf_encontrado"))),
                    )
                    for auto in autos
                    if auto.get("numero")
                ],
            )
            con.execute(
                "INSERT INTO votos_fts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    registro.get("nome_arquivo") or "",
                    registro.get("decisao_instancia") or "",
                    registro.get("protocolo") or "",
                    registro.get("assunto") or "",
                    registro.get("texto") or "",
                    _autos_texto(autos),
                ),
            )
            total += 1
        con.executemany(
            "INSERT INTO meta VALUES (?, ?)",
            [
                ("gerado_em", time.strftime("%Y-%m-%dT%H:%M:%S%z")),
                ("origem", origem),
                ("votos", str(total)),
                ("pulados", str(pulados)),
            ],
        )
        con.execute("INSERT INTO votos_fts(votos_fts) VALUES ('optimize')")
        con.commit()
    finally:
        con.close()
    temporario.replace(saida)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jsonl", type=Path, default=Path("site_data/votos.jsonl"))
    parser.add_argument("--saida", type=Path, default=Path("indice_publico.db"))
    parser.add_argument("--ia", type=Path, default=Path("site_data/ia"))
    parser.add_argument("--marco", type=Path, default=Path("marco_atualizacao.json"))
    parser.add_argument(
        "--arquivos-de",
        type=Path,
        default=None,
        help="indice_busca.db do pipeline: só para ler o caminho relativo dos originais.",
    )
    args = parser.parse_args(argv)

    if not args.jsonl.exists():
        print(f"JSONL não encontrado: {args.jsonl}")
        return 2
    inicio = time.time()
    total = gerar_indice(
        ler_jsonl(args.jsonl),
        args.saida,
        resultados=resultados_da_ia(args.ia),
        arquivos=arquivos_do_indice_privado(args.arquivos_de) if args.arquivos_de else None,
        pulados=pulados_do_marco(args.marco),
        origem=str(args.jsonl),
    )
    print(f"Índice público: {total} decisões em {args.saida} ({time.time() - inicio:.1f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
