#!/usr/bin/env python3
"""
Cria um acervo bruto sem duplicar votos por ato e gera um índice de busca.

Entrada principal: pipeline_cache.db, preenchido pelo pipeline
baixar_e_organizar_por_ato.py. Para cada arquivo descoberto no Drive, este
script baixa uma cópia única em votos_brutos/<instancia>/, extrai o texto do
voto, consulta todos os autos citados no SIF e salva:

  - indice_busca.db: SQLite com FTS5 para busca textual.
  - site_data/votos.jsonl: um registro JSON por voto, pronto para alimentar
    um site público/estático.

O voto é indexado uma vez, mas pode ter vários autos, cada auto com autuado,
infração, dispositivo legal transgredido e lei.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pdfplumber
import requests

from baixar_e_organizar_por_ato import (
    AUTO_RE,
    AUTO_URL,
    LOGIN_PASS,
    LOGIN_URL,
    LOGIN_USER,
    CacheDB,
    DownloadItem,
    _DECISAO_RE,
    _do_download,
    _extract_assunto,
    _extract_protocolo,
    _extrair_texto,
    _load_creds,
    _new_service,
    _retry,
    _san,
    motivo_documento_de_sessao_nao_decisao,
)

MARCO_DEFAULT = "marco_atualizacao.json"
PUBLIC_DRIVE_FIELDS = (
    "drive_file_id_publico",
    "drive_view_url",
    "drive_preview_url",
)


@dataclass
class AutoInfo:
    numero: str
    idn: str
    tipo: str
    autuado: str = ""
    infracao: str = ""
    dispositivo_legal_transgredido: str = ""
    local_constatacao: str = ""
    lei: str = ""
    pdf_encontrado: bool = False


@dataclass
class VotoRecord:
    file_id: str
    nome_arquivo: str
    instancia: str
    decisao_instancia: str
    caminho_bruto: str
    protocolo: str
    assunto: str
    texto: str
    autos: list[AutoInfo]


@dataclass
class ProcessResult:
    status: str
    file_id: str
    nome_arquivo: str
    record: VotoRecord | None = None
    motivo: str = ""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[marco] não foi possível ler {path}: {exc}", flush=True)
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def _processed_ids_from_marco(marco: dict) -> set[str]:
    processed = marco.get("processed_file_ids", {})
    ids: set[str] = set()
    if isinstance(processed, dict):
        for value in processed.values():
            if isinstance(value, list):
                ids.update(str(item) for item in value if item)
    elif isinstance(processed, list):
        ids.update(str(item) for item in processed if item)
    return ids


def _public_links_by_file_id(path: Path) -> dict[str, dict[str, str]]:
    links: dict[str, dict[str, str]] = {}
    if not path.exists():
        return links
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                record = json.loads(line)
                file_id = record.get("file_id")
                if not file_id:
                    continue
                payload = {
                    field: record[field]
                    for field in PUBLIC_DRIVE_FIELDS
                    if record.get(field)
                }
                if payload:
                    links[str(file_id)] = payload
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[jsonl] não foi possível reaproveitar links públicos: {exc}", flush=True)
    return links


def _jsonl_stats(path: Path) -> dict[str, int]:
    stats = {"registros": 0, "com_link_publico": 0}
    if not path.exists():
        return stats
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                stats["registros"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("drive_view_url") or record.get("drive_preview_url"):
                    stats["com_link_publico"] += 1
    except OSError as exc:
        print(f"[jsonl] não foi possível contar {path}: {exc}", flush=True)
    return stats


def _count_files(root: Path) -> int:
    if not root.exists():
        return 0
    return sum(1 for path in root.rglob("*") if path.is_file())


def _cache_file_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        con = sqlite3.connect(path)
        try:
            return int(con.execute("SELECT COUNT(*) FROM arquivos").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def _norm_label(value: str) -> str:
    import unicodedata

    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.casefold()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _clean_value(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" :-")
    words = value.split()
    if len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] == words[half:]:
            value = " ".join(words[:half])
    return value


def _cell_value(cell: str, label: str) -> str:
    if not cell:
        return ""
    lines = [ln.strip() for ln in str(cell).splitlines() if ln.strip()]
    if not lines:
        return ""
    label_norm = _norm_label(label)
    first_norm = _norm_label(lines[0])
    if label_norm not in first_norm:
        return ""
    if len(lines) > 1:
        return _clean_value(" ".join(lines[1:]))
    if ":" in lines[0]:
        return _clean_value(lines[0].split(":", 1)[1])
    return ""


def _extract_from_tables(pdf: pdfplumber.PDF) -> dict[str, str]:
    fields = {
        "autuado": "",
        "infracao": "",
        "dispositivo_legal_transgredido": "",
        "local_constatacao": "",
    }
    labels = {
        "autuado": "NOME RAZAO SOCIAL OU PESSOA FISICA",
        "infracao": "ATO OU FATO CONSTITUTIVO DA INFRACAO",
        "dispositivo_legal_transgredido": "DISPOSITIVO LEGAL TRANSGREDIDO",
        "local_constatacao": "LOCAL DA CONSTATACAO DA INFRACAO ENDERECO COMPLETO",
    }

    for page in pdf.pages:
        for table in page.extract_tables() or []:
            for row in table or []:
                local = ""
                bairro = ""
                for cell in row or []:
                    if not cell:
                        continue
                    for key, label in labels.items():
                        if fields[key]:
                            continue
                        value = _cell_value(cell, label)
                        if value:
                            if key == "local_constatacao":
                                local = value
                            else:
                                fields[key] = value
                    if local and not bairro:
                        bairro = _cell_value(cell, "BAIRRO")
                if local and not fields["local_constatacao"]:
                    fields["local_constatacao"] = (
                        f"{local} - {bairro}" if bairro and bairro not in local else local
                    )
    return fields


def _regex_field(text: str, label: str, stop_labels: Iterable[str]) -> str:
    stop = "|".join(stop_labels)
    pattern = re.compile(
        rf"{label}\s*:?\s*(.+?)(?=\n\s*(?:{stop})\s*:|\n\s*\d{{2}}\s+-|\Z)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(text)
    return _clean_value(match.group(1)) if match else ""


def _extract_from_text(text: str) -> dict[str, str]:
    return {
        "autuado": _regex_field(
            text,
            r"NOME\s*\(RAZ[ÃA]O\s+SOCIAL\s+OU\s+PESSOA\s+F[ÍI]SICA\)",
            ["NOME FANTASIA", "CNPJ/CPF", "CPF/CNPJ", "DML", "ATIVIDADE EXERCIDA"],
        ),
        "infracao": _regex_field(
            text,
            r"ATO\s+OU\s+FATO\s+CONSTITUTIVO\s+DA\s+INFRA[ÇC][ÃA]O",
            ["DESCRI[ÇC][ÃA]O COMPLEMENTAR", "DISPOSITIVO LEGAL TRANSGREDIDO"],
        ),
        "dispositivo_legal_transgredido": _regex_field(
            text,
            r"DISPOSITIVO\s+LEGAL\s+TRANSGREDIDO",
            [
                "DATA DE VISTORIA",
                "DADOS DO VE[ÍI]CULO",
                "PELO PRESENTE",
                "PRAZO PARA CUMPRIMENTO",
                "LOCAL DA CONSTATA[ÇC][ÃA]O",
            ],
        ),
        "local_constatacao": _regex_field(
            text,
            r"LOCAL\s+DA\s+CONSTATA[ÇC][ÃA]O\s+DA\s+INFRA[ÇC][ÃA]O\s*\(ENDERE[ÇC]O\s+COMPLETO\)",
            [
                "PENALIDADE",
                "VALOR BASE",
                "DETALHAMENTO DA MULTA",
                "NA REINCID[ÊE]NCIA",
            ],
        ),
    }


def _first_lei(dispositivo: str) -> str:
    match = re.search(r"\bLEI\s+\d+[\./]\d+\b", dispositivo or "", re.IGNORECASE)
    return re.sub(r"\s+", " ", match.group(0).upper()).strip() if match else ""


def _parse_auto_pdf(content: bytes) -> AutoInfo:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        fields = _extract_from_tables(pdf)

    fallback = _extract_from_text(text)
    for key, value in fallback.items():
        fields[key] = fields[key] or value

    dispositivo = fields["dispositivo_legal_transgredido"]
    return AutoInfo(
        numero="",
        idn="",
        tipo="",
        autuado=fields["autuado"],
        infracao=fields["infracao"],
        dispositivo_legal_transgredido=dispositivo,
        local_constatacao=fields["local_constatacao"],
        lei=_first_lei(dispositivo),
        pdf_encontrado=True,
    )


class IndexStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")
        self._setup()

    def _setup(self) -> None:
        with self.con:
            self.con.executescript(
                """
                CREATE TABLE IF NOT EXISTS votos (
                    file_id            TEXT PRIMARY KEY,
                    nome_arquivo       TEXT NOT NULL,
                    instancia          TEXT NOT NULL,
                    decisao_instancia  TEXT NOT NULL,
                    caminho_bruto      TEXT NOT NULL,
                    protocolo          TEXT,
                    assunto            TEXT,
                    texto              TEXT,
                    indexed_at         REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS autos (
                    voto_file_id                    TEXT NOT NULL,
                    numero                          TEXT NOT NULL,
                    idn                             TEXT NOT NULL,
                    tipo                            TEXT NOT NULL,
                    autuado                         TEXT,
                    infracao                        TEXT,
                    dispositivo_legal_transgredido  TEXT,
                    local_constatacao               TEXT,
                    lei                             TEXT,
                    pdf_encontrado                  INTEGER NOT NULL,
                    PRIMARY KEY (voto_file_id, numero)
                );

                CREATE TABLE IF NOT EXISTS auto_cache (
                    numero                          TEXT PRIMARY KEY,
                    idn                             TEXT NOT NULL,
                    tipo                            TEXT NOT NULL,
                    autuado                         TEXT,
                    infracao                        TEXT,
                    dispositivo_legal_transgredido  TEXT,
                    local_constatacao               TEXT,
                    lei                             TEXT,
                    pdf_encontrado                  INTEGER NOT NULL,
                    updated_at                      REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS skips (
                    file_id       TEXT PRIMARY KEY,
                    nome_arquivo  TEXT NOT NULL,
                    motivo        TEXT NOT NULL,
                    updated_at    REAL NOT NULL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS votos_fts USING fts5(
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
            )
            self._ensure_column("autos", "local_constatacao", "TEXT")
            self._ensure_column("auto_cache", "local_constatacao", "TEXT")

    def _ensure_column(self, table: str, column: str, column_type: str) -> None:
        columns = {
            row[1]
            for row in self.con.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def get_auto(self, numero: str) -> AutoInfo | None:
        with self._lock:
            row = self.con.execute(
                """
                SELECT numero, idn, tipo, autuado, infracao,
                       dispositivo_legal_transgredido, local_constatacao,
                       lei, pdf_encontrado
                  FROM auto_cache
                 WHERE numero = ?
                """,
                (numero,),
            ).fetchone()
        if not row:
            return None
        return AutoInfo(
            numero=row[0],
            idn=row[1],
            tipo=row[2],
            autuado=row[3] or "",
            infracao=row[4] or "",
            dispositivo_legal_transgredido=row[5] or "",
            local_constatacao=row[6] or "",
            lei=row[7] or "",
            pdf_encontrado=bool(row[8]),
        )

    def save_auto(self, info: AutoInfo) -> None:
        with self._lock, self.con:
            self.con.execute(
                """
                INSERT OR REPLACE INTO auto_cache
                (numero, idn, tipo, autuado, infracao,
                 dispositivo_legal_transgredido, local_constatacao,
                 lei, pdf_encontrado, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    info.numero,
                    info.idn,
                    info.tipo,
                    info.autuado,
                    info.infracao,
                    info.dispositivo_legal_transgredido,
                    info.local_constatacao,
                    info.lei,
                    1 if info.pdf_encontrado else 0,
                    time.time(),
                ),
            )

    def save_skip(self, file_id: str, nome_arquivo: str, motivo: str) -> None:
        with self._lock, self.con:
            self.con.execute(
                "INSERT OR REPLACE INTO skips VALUES (?, ?, ?, ?)",
                (file_id, nome_arquivo, motivo, time.time()),
            )

    def processed_file_ids(self) -> set[str]:
        with self._lock:
            ids = {
                row[0]
                for row in self.con.execute("SELECT file_id FROM votos").fetchall()
            }
            ids.update(
                row[0]
                for row in self.con.execute("SELECT file_id FROM skips").fetchall()
            )
        return ids

    def processed_file_ids_by_kind(self) -> dict[str, list[str]]:
        with self._lock:
            votos = [
                row[0]
                for row in self.con.execute(
                    "SELECT file_id FROM votos ORDER BY file_id"
                ).fetchall()
            ]
            skips = [
                row[0]
                for row in self.con.execute(
                    "SELECT file_id FROM skips ORDER BY file_id"
                ).fetchall()
            ]
        return {"votos": votos, "skips": skips}

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "votos_indexados": int(
                    self.con.execute("SELECT COUNT(*) FROM votos").fetchone()[0]
                ),
                "autos": int(
                    self.con.execute("SELECT COUNT(*) FROM autos").fetchone()[0]
                ),
                "autos_cache": int(
                    self.con.execute("SELECT COUNT(*) FROM auto_cache").fetchone()[0]
                ),
                "pulados": int(
                    self.con.execute("SELECT COUNT(*) FROM skips").fetchone()[0]
                ),
            }

    def save_voto(self, record: VotoRecord) -> None:
        autos_texto = "\n".join(
            " ".join(
                part
                for part in (
                    auto.numero,
                    auto.autuado,
                    auto.infracao,
                    auto.dispositivo_legal_transgredido,
                    auto.local_constatacao,
                    auto.lei,
                )
                if part
            )
            for auto in record.autos
        )
        with self._lock, self.con:
            self.con.execute("DELETE FROM autos WHERE voto_file_id = ?", (record.file_id,))
            self.con.execute("DELETE FROM votos WHERE file_id = ?", (record.file_id,))
            self.con.execute("DELETE FROM votos_fts WHERE file_id = ?", (record.file_id,))
            self.con.execute(
                """
                INSERT INTO votos
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.file_id,
                    record.nome_arquivo,
                    record.instancia,
                    record.decisao_instancia,
                    record.caminho_bruto,
                    record.protocolo,
                    record.assunto,
                    record.texto,
                    time.time(),
                ),
            )
            self.con.executemany(
                """
                INSERT INTO autos
                (voto_file_id, numero, idn, tipo, autuado, infracao,
                 dispositivo_legal_transgredido, local_constatacao, lei,
                 pdf_encontrado)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.file_id,
                        auto.numero,
                        auto.idn,
                        auto.tipo,
                        auto.autuado,
                        auto.infracao,
                        auto.dispositivo_legal_transgredido,
                        auto.local_constatacao,
                        auto.lei,
                        1 if auto.pdf_encontrado else 0,
                    )
                    for auto in record.autos
                ],
            )
            self.con.execute(
                """
                INSERT INTO votos_fts
                (file_id, nome_arquivo, decisao_instancia, protocolo, assunto, texto, autos_texto)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.file_id,
                    record.nome_arquivo,
                    record.decisao_instancia,
                    record.protocolo,
                    record.assunto,
                    record.texto,
                    autos_texto,
                ),
            )
            self.con.execute("DELETE FROM skips WHERE file_id = ?", (record.file_id,))

    def export_jsonl(self, path: Path, include_text: bool = True) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        public_links = _public_links_by_file_id(path)
        count = 0
        with self._lock:
            votos = self.con.execute(
                """
                SELECT file_id, nome_arquivo, instancia, decisao_instancia,
                       caminho_bruto, protocolo, assunto, texto
                  FROM votos
                 ORDER BY decisao_instancia, nome_arquivo
                """
            ).fetchall()
            with tmp.open("w", encoding="utf-8") as fh:
                for row in votos:
                    autos = self.con.execute(
                        """
                        SELECT numero, idn, tipo, autuado, infracao,
                               dispositivo_legal_transgredido, local_constatacao,
                               lei, pdf_encontrado
                          FROM autos
                         WHERE voto_file_id = ?
                         ORDER BY numero
                        """,
                        (row[0],),
                    ).fetchall()
                    payload = {
                        "file_id": row[0],
                        "nome_arquivo": row[1],
                        "instancia": row[2],
                        "decisao_instancia": row[3],
                        "caminho_bruto": row[4],
                        "protocolo": row[5] or "",
                        "assunto": row[6] or "",
                        "autos": [
                            asdict(
                                AutoInfo(
                                    numero=a[0],
                                    idn=a[1],
                                    tipo=a[2],
                                    autuado=a[3] or "",
                                    infracao=a[4] or "",
                                    dispositivo_legal_transgredido=a[5] or "",
                                    local_constatacao=a[6] or "",
                                    lei=a[7] or "",
                                    pdf_encontrado=bool(a[8]),
                                )
                            )
                            for a in autos
                        ],
                    }
                    if include_text:
                        payload["texto"] = row[7] or ""
                    payload.update(public_links.get(row[0], {}))
                    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    count += 1
        tmp.replace(path)
        return count

    def close(self) -> None:
        with self._lock:
            self.con.close()


class AutoClient:
    def __init__(self, store: IndexStore, rate: float):
        self.store = store
        self._session: requests.Session | None = None
        self._session_lock = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._last = 0.0
        self._interval = 1.0 / max(rate, 0.1)

    def _session_(self) -> requests.Session:
        with self._session_lock:
            if self._session is None:
                session = requests.Session()
                session.headers["User-Agent"] = "Mozilla/5.0"
                try:
                    session.post(
                        LOGIN_URL,
                        data={
                            "login": LOGIN_USER,
                            "password": LOGIN_PASS,
                            "Button_DoLogin": "Login",
                        },
                        timeout=30,
                    ).raise_for_status()
                except Exception as exc:
                    print(f"[SIF] login: {exc}", flush=True)
                self._session = session
            return self._session

    def _throttle(self) -> None:
        with self._throttle_lock:
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def consultar_auto(self, idn: str, tipo: str, refresh: bool = False) -> AutoInfo:
        numero = f"{idn}{tipo}"
        if not refresh:
            cached = self.store.get_auto(numero)
            if cached:
                return cached

        def attempt() -> AutoInfo:
            self._throttle()
            session = self._session_()
            response = session.get(
                AUTO_URL,
                params={"Idn_Doct_Lavr": idn, "Tip_Auto": tipo},
                timeout=30,
            )
            response.raise_for_status()
            idx = response.content.find(b"%PDF")
            if idx == -1:
                return AutoInfo(numero=numero, idn=idn, tipo=tipo)
            info = _parse_auto_pdf(response.content[idx:])
            info.numero = numero
            info.idn = idn
            info.tipo = tipo
            return info

        info = _retry(attempt, what=f"SIF {numero}")
        self.store.save_auto(info)
        return info


class DriveDownloader:
    def __init__(self, token_path: Path):
        self.token_path = token_path
        self._local = threading.local()

    def service(self):
        if not getattr(self._local, "service", None):
            self._local.service = _new_service(self.token_path)
        return self._local.service

    def ensure_file(self, original: DownloadItem, raw_item: DownloadItem) -> str:
        if _raw_exists(raw_item.destination, raw_item.file_size):
            return "exists"
        if original.destination.exists():
            raw_item.destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original.destination, raw_item.destination)
            return "copied"
        _do_download(self.service(), raw_item)
        return "downloaded"


def _raw_exists(path: Path, size: str | None) -> bool:
    if not path.exists():
        return False
    if not size:
        return path.stat().st_size > 0
    try:
        return path.stat().st_size == int(size)
    except (OSError, ValueError):
        return path.stat().st_size > 0


def _raw_item(item: DownloadItem, raw_dir: Path) -> DownloadItem:
    ext = item.destination.suffix or Path(item.file_name).suffix
    stem = _san(Path(item.file_name).stem)[:140]
    filename = f"{stem} - {item.file_id[:8]}{ext}"
    destination = raw_dir / item.instancia / filename
    return DownloadItem(item.file_id, item.file_name, item.file_size, destination, item.instancia)


def _decisao_instancia(instancia: str) -> str:
    if instancia.startswith("1"):
        return "Decisão de 1ª instância"
    if instancia.startswith("2"):
        return "Decisão de 2ª instância"
    return "Decisão"


def _autos_unicos(texto: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    autos: list[tuple[str, str]] = []
    for idn, tipo in AUTO_RE.findall(texto):
        numero = f"{idn}{tipo}"
        if numero in seen:
            continue
        seen.add(numero)
        autos.append((idn, tipo))
    return autos


def process_item(
    item: DownloadItem,
    raw_dir: Path,
    downloader: DriveDownloader,
    autos: AutoClient,
    refresh_autos: bool,
    remover_nao_decisoes: bool,
) -> ProcessResult:
    raw_item = _raw_item(item, raw_dir)
    downloader.ensure_file(item, raw_item)

    texto = _extrair_texto(raw_item)
    if texto is None:
        return ProcessResult("skip", item.file_id, item.file_name, motivo="texto nao extraido")

    motivo = motivo_documento_de_sessao_nao_decisao(item.file_name, texto)
    if motivo:
        if remover_nao_decisoes:
            raw_item.destination.unlink(missing_ok=True)
        return ProcessResult("skip", item.file_id, item.file_name, motivo=motivo)

    if not _DECISAO_RE.search(texto):
        if remover_nao_decisoes:
            raw_item.destination.unlink(missing_ok=True)
        return ProcessResult("skip", item.file_id, item.file_name, motivo="sem dispositivo da decisao")

    assunto = _extract_assunto(texto)
    auto_infos = [
        autos.consultar_auto(idn, tipo, refresh=refresh_autos)
        for idn, tipo in _autos_unicos(assunto)
    ]
    record = VotoRecord(
        file_id=item.file_id,
        nome_arquivo=item.file_name,
        instancia=item.instancia,
        decisao_instancia=_decisao_instancia(item.instancia),
        caminho_bruto=str(raw_item.destination),
        protocolo=_extract_protocolo(item.file_name, texto),
        assunto=assunto,
        texto=texto,
        autos=auto_infos,
    )
    return ProcessResult("ok", item.file_id, item.file_name, record=record)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cria votos_brutos e índice enriquecido para o site público."
    )
    parser.add_argument("--cache", default="pipeline_cache.db")
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--token", default="token.json")
    parser.add_argument("--votos-brutos", default="votos_brutos")
    parser.add_argument("--indice", default="indice_busca.db")
    parser.add_argument("--jsonl", default="site_data/votos.jsonl")
    parser.add_argument("--marco", default=MARCO_DEFAULT)
    parser.add_argument("--instancia", choices=["1", "2", "ambas"], default="ambas")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--sif-rate", type=float, default=5.0)
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=15.0,
        help="Intervalo, em segundos, entre resumos de progresso.",
    )
    parser.add_argument(
        "--quiet-items",
        action="store_true",
        help="Não imprime uma linha por arquivo; mostra apenas resumos de progresso.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Processa só N arquivos, para teste.")
    parser.add_argument(
        "--file-id",
        action="append",
        default=[],
        help="Processa apenas o(s) file_id(s) informado(s). Pode repetir.",
    )
    parser.add_argument(
        "--refresh-autos",
        action="store_true",
        help="Ignora o cache de autos do índice e reconsulta o SIF.",
    )
    parser.add_argument(
        "--manter-nao-decisoes",
        action="store_true",
        help="Mantém em votos_brutos arquivos que não forem decisões.",
    )
    parser.add_argument(
        "--json-sem-texto",
        action="store_true",
        help="Exporta JSONL sem o texto integral do voto.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Apaga o índice SQLite atual antes de reprocessar os arquivos selecionados.",
    )
    parser.add_argument(
        "--continuar",
        action="store_true",
        help="Também usa votos/skips do índice SQLite atual para retomar.",
    )
    parser.add_argument(
        "--ignorar-marco",
        action="store_true",
        help="Não usa o arquivo de marco para pular file_ids nesta execução.",
    )
    return parser.parse_args()


def _filtrar_items(items: list[DownloadItem], args: argparse.Namespace) -> list[DownloadItem]:
    if args.instancia != "ambas":
        prefix = "1" if args.instancia == "1" else "2"
        items = [item for item in items if item.instancia.startswith(prefix)]
    if args.file_id:
        wanted = set(args.file_id)
        items = [item for item in items if item.file_id in wanted]
    if args.limit > 0:
        items = items[: args.limit]
    return items


def _build_marco(
    *,
    store: IndexStore,
    existing: dict,
    cache_path: Path,
    votos_brutos_path: Path,
    indice_path: Path,
    jsonl_path: Path,
    args: argparse.Namespace,
) -> dict:
    ids_by_kind = store.processed_file_ids_by_kind()
    if not ids_by_kind["votos"] and not ids_by_kind["skips"]:
        previous = existing.get("processed_file_ids", {})
        if isinstance(previous, dict):
            ids_by_kind = {
                "votos": sorted(str(item) for item in previous.get("votos", []) if item),
                "skips": sorted(str(item) for item in previous.get("skips", []) if item),
            }

    jsonl = _jsonl_stats(jsonl_path)
    counts = store.counts()
    previous_counts = existing.get("counts") or {}
    if not ids_by_kind["votos"] and not ids_by_kind["skips"] and isinstance(previous_counts, dict):
        for key in ("votos_indexados", "autos", "autos_cache", "pulados"):
            if counts.get(key) == 0 and previous_counts.get(key):
                counts[key] = int(previous_counts[key])
    counts.update(
        {
            "jsonl_registros": jsonl["registros"],
            "jsonl_com_link_publico": jsonl["com_link_publico"],
            "votos_brutos_arquivos": _count_files(votos_brutos_path),
            "arquivos_cache": _cache_file_count(cache_path),
            "processados_no_marco": len(ids_by_kind["votos"]) + len(ids_by_kind["skips"]),
        }
    )

    public_drive = dict(existing.get("public_drive") or {})
    if jsonl["registros"]:
        public_drive["matched"] = jsonl["com_link_publico"]
        public_drive["missing"] = jsonl["registros"] - jsonl["com_link_publico"]
    if public_drive.get("folder_id") is None:
        public_drive.pop("folder_id", None)

    return {
        "schema": 1,
        "updated_at": _now_iso(),
        "last_writer": "preparar_acervo_publico.py",
        "description": (
            "Marco versionado de retomada do acervo público. "
            "IDs em processed_file_ids são pulados nas próximas execuções, "
            "salvo com --rebuild ou --ignorar-marco."
        ),
        "paths": {
            "cache": args.cache,
            "indice": args.indice,
            "jsonl": args.jsonl,
            "votos_brutos": args.votos_brutos,
        },
        "counts": counts,
        "public_drive": public_drive,
        "processed_file_ids": ids_by_kind,
    }


def main() -> int:
    args = parse_args()
    base = Path.cwd()
    resolve = lambda value: Path(value) if Path(value).is_absolute() else base / value

    cache_path = resolve(args.cache)
    if not cache_path.exists():
        print(f"Cache não encontrado: {cache_path}")
        print("Rode primeiro baixar_e_organizar_por_ato.py para preencher o índice do Drive.")
        return 2

    _load_creds(resolve(args.credentials), resolve(args.token))

    indice_path = resolve(args.indice)
    if args.rebuild:
        for suffix in ("", "-wal", "-shm"):
            indice_path.with_name(indice_path.name + suffix).unlink(missing_ok=True)

    store = IndexStore(indice_path)
    cache = CacheDB(cache_path)
    marco_path = resolve(args.marco)
    marco = {} if args.ignorar_marco else _read_json(marco_path)
    items = _filtrar_items(cache.load_arquivos(), args)
    total_geral = len(items)
    already_done = 0
    if not args.rebuild and not args.refresh_autos:
        processed_ids: set[str] = set()
        marco_ids = set()
        db_ids = set()
        if not args.ignorar_marco:
            marco_ids = _processed_ids_from_marco(marco)
            processed_ids.update(marco_ids)
        if args.continuar:
            db_ids = store.processed_file_ids()
            processed_ids.update(db_ids)
        before = len(items)
        items = [item for item in items if item.file_id not in processed_ids]
        already_done = before - len(items)
        if already_done:
            sources = []
            if marco_ids:
                sources.append(f"marco {len(marco_ids)}")
            if db_ids:
                sources.append(f"SQLite {len(db_ids)}")
            origem = " + ".join(sources) if sources else "retomada"
            print(
                f"Retomada: {already_done} arquivos já varridos serão pulados ({origem})."
            )
    if not items:
        jsonl_path = resolve(args.jsonl)
        if store.counts()["votos_indexados"] == 0 and jsonl_path.exists():
            exported = _jsonl_stats(jsonl_path)["registros"]
            print(
                "Índice SQLite sem votos; JSONL existente foi mantido para não apagar "
                "o índice estático."
            )
        else:
            exported = store.export_jsonl(jsonl_path, include_text=not args.json_sem_texto)
        marco_payload = _build_marco(
            store=store,
            existing=marco,
            cache_path=cache_path,
            votos_brutos_path=resolve(args.votos_brutos),
            indice_path=indice_path,
            jsonl_path=jsonl_path,
            args=args,
        )
        _write_json_atomic(marco_path, marco_payload)
        store.close()
        print(f"Nenhum arquivo novo para processar. JSONL atualizado com {exported} registros.")
        print(f"Marco atualizado      : {marco_path}")
        return 0
    downloader = DriveDownloader(resolve(args.token))
    auto_client = AutoClient(store, rate=args.sif_rate)

    print(f"Arquivos selecionados : {len(items)}")
    print(f"Votos brutos          : {resolve(args.votos_brutos)}")
    print(f"Índice SQLite         : {resolve(args.indice)}")
    print(f"JSONL                 : {resolve(args.jsonl)}")
    print(f"Workers               : {args.workers}")
    print()

    ok = 0
    skipped = 0
    errors = 0
    started = time.time()
    last_progress = 0.0

    def print_progress(force: bool = False) -> None:
        nonlocal last_progress
        now = time.time()
        if not force and now - last_progress < max(args.progress_interval, 1.0):
            return
        processed = ok + skipped + errors
        overall_processed = already_done + processed
        elapsed = max(now - started, 0.001)
        rate_min = processed / elapsed * 60
        remaining = max(len(items) - processed, 0)
        eta_min = remaining / rate_min if rate_min > 0 else 0.0
        pct = overall_processed / total_geral * 100
        print(
            f"[progresso] varridos {overall_processed}/{total_geral} ({pct:.1f}%) | "
            f"rodada {processed}/{len(items)} | "
            f"indexados {ok} | pulados {skipped} | erros {errors} | "
            f"{rate_min:.1f} arq/min | ETA {eta_min:.1f} min",
            flush=True,
        )
        last_progress = now

    print_progress(force=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [
            pool.submit(
                process_item,
                item,
                resolve(args.votos_brutos),
                downloader,
                auto_client,
                args.refresh_autos,
                not args.manter_nao_decisoes,
            )
            for item in items
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            try:
                result = future.result()
            except Exception as exc:
                errors += 1
                print(f"[erro] {exc}", flush=True)
                continue

            if result.status == "ok" and result.record:
                store.save_voto(result.record)
                ok += 1
                auto_count = len(result.record.autos)
                if not args.quiet_items:
                    print(
                        f"[{index}/{len(items)}] ok: {result.nome_arquivo} "
                        f"({auto_count} auto{'s' if auto_count != 1 else ''})",
                        flush=True,
                    )
            else:
                store.save_skip(result.file_id, result.nome_arquivo, result.motivo)
                skipped += 1
                if not args.quiet_items:
                    print(
                        f"[{index}/{len(items)}] pula: {result.nome_arquivo} "
                        f"({result.motivo})",
                        flush=True,
                    )
            print_progress()

    exported = store.export_jsonl(resolve(args.jsonl), include_text=not args.json_sem_texto)
    marco_payload = _build_marco(
        store=store,
        existing=marco,
        cache_path=cache_path,
        votos_brutos_path=resolve(args.votos_brutos),
        indice_path=indice_path,
        jsonl_path=resolve(args.jsonl),
        args=args,
    )
    _write_json_atomic(marco_path, marco_payload)
    store.close()

    elapsed = time.time() - started
    print()
    print("Concluído.")
    print(f"Votos indexados       : {ok}")
    print(f"Pulados               : {skipped}")
    print(f"Erros                 : {errors}")
    print(f"Registros no JSONL    : {exported}")
    print(f"Marco atualizado      : {marco_path}")
    print(f"Tempo                 : {elapsed/60:.1f} min")
    print_progress(force=True)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
