"""Busca FTS5 no índice público. As consultas são as do antigo site_server.py."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


class ConsultaInvalida(ValueError):
    """A consulta não é uma expressão FTS5 válida."""


@dataclass(frozen=True)
class ParametrosBusca:
    q: str = ""
    instancia: str = "todas"
    modo: str = "phrase"
    mes: str = ""
    ano: str = ""
    limit: int = 50
    offset: int = 0


def _tokens(query: str) -> list[str]:
    return re.findall(r"[\w]+", query, flags=re.UNICODE)


def fts_query(query: str, mode: str = "phrase") -> str:
    """Expressão FTS5: frase exata ou todas as palavras como prefixo."""
    if mode in {"words", "any"}:
        return " AND ".join(f'"{token}"*' for token in _tokens(query))
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


def date_fts_query(mes: str, ano: str) -> str:
    """Filtro de data como frase ("março de 2025"), igual ao do modo estático."""
    if not ano:
        return ""
    phrase = (f"{mes} de {ano}" if mes else f"de {ano}").replace('"', '""')
    return f'"{phrase}"'


def file_url(file_id: str) -> str:
    return f"/api/file/{quote(file_id, safe='')}"


def _drive(file_id: str, sufixo: str) -> str:
    return f"https://drive.google.com/file/d/{quote(file_id, safe='')}/{sufixo}"


def _ext(nome: str) -> str:
    return Path(nome).suffix.lower().lstrip(".")


def _com_urls(item: dict) -> dict:
    item["file_url"] = file_url(item["file_id"])
    publico = item.get("drive_file_id_publico")
    item["drive_view_url"] = _drive(publico, "view") if publico else None
    item["drive_preview_url"] = _drive(publico, "preview") if publico else None
    item["ext"] = _ext(item.get("nome_arquivo") or "")
    return item


def autos(con: sqlite3.Connection, file_id: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT numero, infracao, dispositivo_legal_transgredido,
               local_constatacao, lei, pdf_encontrado
          FROM autos
         WHERE voto_file_id = ?
         ORDER BY numero
        """,
        (file_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def stats(con: sqlite3.Connection) -> dict:
    pulados = con.execute("SELECT valor FROM meta WHERE chave = 'pulados'").fetchone()
    return {
        "votos": con.execute("SELECT count(*) FROM votos").fetchone()[0],
        "autos": con.execute("SELECT count(*) FROM autos").fetchone()[0],
        "pulados": int(pulados[0]) if pulados else 0,
    }


_CAMPOS = """v.file_id, v.nome_arquivo, v.instancia, v.decisao_instancia, v.protocolo,
             v.assunto, v.data, v.resultado, v.drive_file_id_publico"""


def buscar(con: sqlite3.Connection, p: ParametrosBusca) -> dict:
    """Busca paginada: FTS5 com ranking BM25 ou, sem consulta, as decisões mais recentes."""
    where_instancia = ""
    if p.instancia in {"1a_instancia", "2a_instancia"}:
        where_instancia = " AND v.instancia = ?"
    filtro = [p.instancia] if where_instancia else []

    query = p.q.strip()
    date_match = date_fts_query(p.mes.strip(), p.ano.strip())
    try:
        if query or date_match:
            parts = [fts_query(query, p.modo)] if query else []
            if date_match:
                parts.append(date_match)
            match = " ".join(part for part in parts if part)
            if not match:
                return {"items": [], "total": 0, "limit": p.limit, "offset": p.offset}
            total = con.execute(
                "SELECT count(*) FROM votos_fts f JOIN votos v ON v.file_id = f.file_id "
                "WHERE votos_fts MATCH ?" + where_instancia,
                [match, *filtro],
            ).fetchone()[0]
            rows = con.execute(
                f"""
                SELECT {_CAMPOS},
                       snippet(votos_fts, -1, '<mark>', '</mark>', '...', 42) AS trecho,
                       bm25(votos_fts) AS rank
                  FROM votos_fts f
                  JOIN votos v ON v.file_id = f.file_id
                 WHERE votos_fts MATCH ?{where_instancia}
                 ORDER BY rank
                 LIMIT ? OFFSET ?
                """,
                [match, *filtro, p.limit, p.offset],
            ).fetchall()
        else:
            total = con.execute("SELECT count(*) FROM votos v WHERE 1=1" + where_instancia, filtro).fetchone()[0]
            # Sem consulta: as mais recentes (o índice público não tem a data de indexação
            # do pipeline; a data da decisão faz o mesmo papel).
            rows = con.execute(
                f"""
                SELECT {_CAMPOS}, substr(v.texto, 1, 420) AS trecho, 0 AS rank
                  FROM votos v
                 WHERE 1=1{where_instancia}
                 ORDER BY v.data IS NULL, v.data DESC, v.rowid
                 LIMIT ? OFFSET ?
                """,
                [*filtro, p.limit, p.offset],
            ).fetchall()
    except sqlite3.OperationalError as exc:
        if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
            raise ConsultaInvalida(str(exc)) from exc
        raise

    items = []
    for row in rows:
        item = _com_urls(dict(row))
        item["trecho"] = item["trecho"] or ""
        item["autos"] = autos(con, item["file_id"])
        items.append(item)
    return {"items": items, "total": total, "limit": p.limit, "offset": p.offset}


def voto(con: sqlite3.Connection, file_id: str) -> dict | None:
    row = con.execute(f"SELECT {_CAMPOS.replace('v.', '')}, texto FROM votos WHERE file_id = ?", (file_id,)).fetchone()
    if not row:
        return None
    payload = _com_urls(dict(row))
    payload["texto_preview"] = payload.pop("texto") or ""
    payload["autos"] = autos(con, file_id)
    return payload


def caminho_do_arquivo(con: sqlite3.Connection, file_id: str, raiz: Path) -> tuple[Path, str] | None:
    """Caminho do original dentro de `raiz` e o nome (mascarado) para o download."""
    row = con.execute("SELECT arquivo, nome_arquivo FROM votos WHERE file_id = ?", (file_id,)).fetchone()
    if not row or not row["arquivo"]:
        return None
    raiz = raiz.resolve()
    caminho = (raiz / row["arquivo"]).resolve()
    if not caminho.is_relative_to(raiz) or not caminho.is_file():
        return None
    return caminho, row["nome_arquivo"]
