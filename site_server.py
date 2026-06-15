#!/usr/bin/env python3
"""
Servidor leve para o site público de busca.

Usa apenas biblioteca padrão: SQLite FTS5 para busca, arquivos estáticos em
site_publico/ e os votos brutos servidos pelo file_id.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sqlite3
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path(__file__).resolve().parent
SITE_DIR = ROOT / "site_publico"
DB_PATH = ROOT / "indice_busca.db"
MAX_LIMIT = 100


def _json_response(handler: SimpleHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler: SimpleHTTPRequestHandler, status: int, message: str) -> None:
    _json_response(handler, {"error": message}, status)


def _tokens(query: str) -> list[str]:
    return re.findall(r"[\w]+", query, flags=re.UNICODE)


def _fts_query(query: str, mode: str = "phrase") -> str:
    tokens = _tokens(query)
    if mode == "any":
        return " OR ".join(f"{token}*" for token in tokens)
    escaped = query.replace('"', '""')
    return f'"{escaped}"'


def _date_fts_query(mes: str, ano: str) -> str:
    if not ano:
        return ""
    phrase = (f"{mes} de {ano}" if mes else f"de {ano}").replace('"', '""')
    return f'"{phrase}"'


def _row_dict(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in row.keys()}


def _file_url(file_id: str) -> str:
    return f"/api/file/{quote(file_id, safe='')}"


def _drive_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{quote(file_id, safe='')}/view"


def _drive_preview_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{quote(file_id, safe='')}/preview"


def _with_urls(payload: dict, file_id: str) -> dict:
    payload["file_url"] = _file_url(file_id)
    payload["drive_view_url"] = _drive_view_url(file_id)
    payload["drive_preview_url"] = _drive_preview_url(file_id)
    return payload


def _ext(path: str) -> str:
    return Path(path).suffix.lower().lstrip(".")


class SiteHandler(SimpleHTTPRequestHandler):
    server_version = "AcervoPublico/1.0"

    def __init__(self, *args, directory: str | None = None, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        print(f"[site] {self.address_string()} - {fmt % args}", flush=True)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.path = "/index.html"
            return super().do_GET()
        if parsed.path == "/api/stats":
            return self._stats()
        if parsed.path == "/api/search":
            return self._search(parsed.query)
        if parsed.path.startswith("/api/voto/"):
            file_id = unquote(parsed.path.removeprefix("/api/voto/"))
            return self._voto(file_id)
        if parsed.path.startswith("/api/file/"):
            file_id = unquote(parsed.path.removeprefix("/api/file/"))
            return self._file(file_id)
        return super().do_GET()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        return con

    def _stats(self) -> None:
        with self._connect() as con:
            payload = {
                "votos": con.execute("SELECT count(*) FROM votos").fetchone()[0],
                "autos": con.execute("SELECT count(*) FROM autos").fetchone()[0],
                "pulados": con.execute("SELECT count(*) FROM skips").fetchone()[0],
            }
        _json_response(self, payload)

    def _search(self, query_string: str) -> None:
        params = parse_qs(query_string)
        query = (params.get("q") or [""])[0].strip()
        instancia = (params.get("instancia") or ["todas"])[0]
        mode = (params.get("modo") or ["phrase"])[0]
        mes = (params.get("mes") or [""])[0].strip()
        ano = (params.get("ano") or [""])[0].strip()
        limit = min(max(int((params.get("limit") or ["50"])[0]), 1), MAX_LIMIT)
        offset = max(int((params.get("offset") or ["0"])[0]), 0)

        where_instancia = ""
        sql_params: list[object] = []
        if instancia in {"1a_instancia", "2a_instancia"}:
            where_instancia = " AND v.instancia = ?"

        with self._connect() as con:
            date_match = _date_fts_query(mes, ano)
            if query or date_match:
                parts = []
                if query:
                    parts.append(_fts_query(query, mode))
                if date_match:
                    parts.append(date_match)
                match = " ".join(parts)
                if not match:
                    return _json_response(self, {"items": [], "total": 0})
                count_sql = (
                    "SELECT count(*) "
                    "FROM votos_fts f JOIN votos v ON v.file_id = f.file_id "
                    "WHERE votos_fts MATCH ?" + where_instancia
                )
                sql_params = [match]
                if where_instancia:
                    sql_params.append(instancia)
                total = con.execute(count_sql, sql_params).fetchone()[0]

                search_sql = (
                    """
                    SELECT v.file_id, v.nome_arquivo, v.instancia, v.decisao_instancia,
                           v.protocolo, v.assunto, v.caminho_bruto,
                           snippet(votos_fts, -1, '<mark>', '</mark>', '...', 42) AS trecho,
                           bm25(votos_fts) AS rank
                      FROM votos_fts f
                      JOIN votos v ON v.file_id = f.file_id
                     WHERE votos_fts MATCH ?
                    """
                    + where_instancia
                    + """
                     ORDER BY rank
                     LIMIT ? OFFSET ?
                    """
                )
                rows = con.execute(search_sql, [*sql_params, limit, offset]).fetchall()
            else:
                total_sql = "SELECT count(*) FROM votos v WHERE 1=1" + where_instancia
                sql_params = [instancia] if where_instancia else []
                total = con.execute(total_sql, sql_params).fetchone()[0]
                rows = con.execute(
                    """
                    SELECT v.file_id, v.nome_arquivo, v.instancia, v.decisao_instancia,
                           v.protocolo, v.assunto, v.caminho_bruto,
                           substr(v.texto, 1, 420) AS trecho,
                           0 AS rank
                      FROM votos v
                     WHERE 1=1
                    """
                    + where_instancia
                    + """
                     ORDER BY v.indexed_at DESC
                     LIMIT ? OFFSET ?
                    """,
                    [*sql_params, limit, offset],
                ).fetchall()

            items = []
            for row in rows:
                item = _row_dict(row)
                _with_urls(item, row["file_id"])
                item["ext"] = _ext(row["caminho_bruto"])
                item["autos"] = self._autos(con, row["file_id"])
                items.append(item)

        _json_response(self, {"items": items, "total": total, "limit": limit, "offset": offset})

    def _autos(self, con: sqlite3.Connection, file_id: str) -> list[dict]:
        rows = con.execute(
            """
            SELECT numero, autuado, infracao, dispositivo_legal_transgredido,
                   local_constatacao, lei, pdf_encontrado
              FROM autos
             WHERE voto_file_id = ?
             ORDER BY numero
            """,
            (file_id,),
        ).fetchall()
        return [_row_dict(row) for row in rows]

    def _voto(self, file_id: str) -> None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT file_id, nome_arquivo, instancia, decisao_instancia,
                       caminho_bruto, protocolo, assunto, texto
                  FROM votos
                 WHERE file_id = ?
                """,
                (file_id,),
            ).fetchone()
            if not row:
                return _error(self, HTTPStatus.NOT_FOUND, "Voto não encontrado")
            payload = _row_dict(row)
            _with_urls(payload, file_id)
            payload["ext"] = _ext(row["caminho_bruto"])
            payload["texto_preview"] = (row["texto"] or "")[:12000]
            payload.pop("texto", None)
            payload["autos"] = self._autos(con, file_id)
        _json_response(self, payload)

    def _file(self, file_id: str) -> None:
        with self._connect() as con:
            row = con.execute(
                "SELECT caminho_bruto, nome_arquivo FROM votos WHERE file_id = ?",
                (file_id,),
            ).fetchone()
        if not row:
            return _error(self, HTTPStatus.NOT_FOUND, "Arquivo não encontrado")

        path = Path(row["caminho_bruto"]).resolve()
        votos_root = (ROOT / "votos_brutos").resolve()
        try:
            path.relative_to(votos_root)
        except ValueError:
            return _error(self, HTTPStatus.FORBIDDEN, "Caminho fora de votos_brutos")
        if not path.exists():
            return _error(self, HTTPStatus.NOT_FOUND, "Arquivo ausente em votos_brutos")

        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        disposition = "inline" if path.suffix.lower() in {".pdf", ".txt"} else "attachment"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header(
            "Content-Disposition",
            f"{disposition}; filename*=UTF-8''{quote(row['nome_arquivo'])}",
        )
        self.end_headers()
        with path.open("rb") as fh:
            shutil_copyfileobj(fh, self.wfile)


def shutil_copyfileobj(src, dst, length: int = 1024 * 1024) -> None:
    while True:
        buf = src.read(length)
        if not buf:
            break
        dst.write(buf)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Servidor do site público do acervo.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not DB_PATH.exists():
        print(f"Índice não encontrado: {DB_PATH}")
        return 2
    server = ThreadingHTTPServer((args.host, args.port), SiteHandler)
    print(f"Site: http://{args.host}:{args.port}/")
    print(f"Busca: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
