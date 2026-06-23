#!/usr/bin/env python3
"""
Pipeline contínuo de 3 estágios rodando simultaneamente:

  [index workers]  →  download_queue  →  [download workers]  →  organize_queue  →  [organize workers]
   lista pastas         fila de arquivos    baixa do Drive         fila prontos        SIF + copia por ato

Assim que uma pasta é indexada, seus arquivos entram na fila de download.
Assim que um arquivo é baixado, entra na fila de organização.
Os 3 estágios ocorrem em paralelo desde o início.

Fluxo ÚNICO, trata PDF e Word (Google Docs, .docx, .doc — escolha via --tipos).
Só a leitura do arquivo muda por formato; o resto do pipeline é idêntico.

Organiza por LEI → ATO: cada auto do voto dá um par (lei, ato), e o voto é
copiado para  <saida>/<instancia>/<LEI>/<ATO>/voto . Ambos vêm do PDF do AUTO
no SIF (uma busca devolve os dois).

O estágio de organização também:
  - descarta não-decisões (sem "DISPOSITIVO DA DECISÃO") ANTES de consultar o
    SIF, então o trabalho caro não é gasto em arquivos que seriam deletados;
  - grava protocolo/assunto/ato de cada decisão no assuntos.csv;
  - nomeia a pasta do ato com até 7 primeiras palavras, removendo conectivos
    finais soltos (como "DA", "E", "OU", "COM"); também remove números/AFERIDA
    (lixo do SIF) e normaliza pra CAIXA-ALTA antes de cortar.

Leitura por formato: PDF→pdfplumber, .docx→docx2txt, .doc→LibreOffice, Google
Docs→exportado como texto pela API. O cache pipeline_cache.db guarda o índice do
Drive e os pares (lei, ato) por voto.

Falhas transitórias de rede (Drive, download, SIF) não abortam a execução:
o pipeline pausa e tenta de novo com backoff (até ~15min por item por padrão);
se não voltar, desiste daquele item, loga e segue.
"""

from __future__ import annotations

import argparse
import csv
import io
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import requests
    import pdfplumber
    HAS_PDF_DEPS = True
except ImportError:
    HAS_PDF_DEPS = False

try:
    import docx2txt          # extração de .docx
except ImportError:
    docx2txt = None

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload


# =============================================================================
# FINE TUNING — ajuste aqui velocidade e resiliência do pipeline.
# São os defaults; cada um ainda pode ser sobrescrito por flag de CLI.
# =============================================================================

class Tuning:
    # Paralelismo: número de workers por estágio
    INDEX_WORKERS    = 60     # listagem de pastas/arquivos no Drive
    DOWNLOAD_WORKERS = 30     # download dos arquivos do Drive
    ORGANIZE_WORKERS = 24     # consulta SIF + organização por ato

    # SIF: consultas por segundo, somando todos os organize workers (throttle global)
    SIF_RATE = 10.0

    # .doc: nº máx. de conversões LibreOffice simultâneas (cada uma sobe um
    # processo pesado; não deixar os organize workers subirem soffice juntos).
    DOC_CONVERT_CONCURRENCY = 3

    # Distribuição por ato: nº de palavras no nome da pasta. Depois do corte,
    # remove conectivos finais soltos (preposições/artigos).
    # Ver nome_pasta_do_ato().
    MAX_PALAVRAS_ATO = 7
    PALAVRAS_FINAIS_SOLTAS = {
        "A", "O", "AS", "OS", "E",
        "DE", "DA", "DO", "DAS", "DOS",
        "EM", "NA", "NO", "NAS", "NOS",
        "AO", "AOS", "OU", "COM", "SEM", "POR",
    }

    # Resiliência: retry com backoff exponencial nas falhas de rede
    MAX_RETRIES       = 20    # tentativas antes de desistir do item; 0 = infinito
    RETRY_BACKOFF     = 2.0   # espera da 1ª tentativa, em s (dobra a cada vez)
    RETRY_BACKOFF_CAP = 60.0  # teto da espera entre tentativas, em s


# =============================================================================
# Configuração
# =============================================================================

SCOPES       = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME  = "application/vnd.google-apps.folder"
PDF_MIME     = "application/pdf"
GDOC_MIME    = "application/vnd.google-apps.document"
DOCX_MIME    = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC_MIME     = "application/msword"

# tipo (CLI --tipos) -> mimeType ; mimeType -> extensão local do arquivo baixado.
# O tipo de cada voto é derivado da extensão (não há coluna mime no cache).
TIPO_MIME = {"pdf": PDF_MIME, "gdoc": GDOC_MIME, "docx": DOCX_MIME, "doc": DOC_MIME}
MIME_EXT  = {PDF_MIME: ".pdf", GDOC_MIME: ".txt", DOCX_MIME: ".docx", DOC_MIME: ".doc"}

SIF_BASE  = "https://sif-piloto.pbh.gov.br"
LOGIN_URL = f"{SIF_BASE}/Login.php?comp=1&ccsForm=Login"
AUTO_URL  = f"{SIF_BASE}/MostraAuto.php"
LOGIN_USER = "saulohr"
LOGIN_PASS = "saulohr"

AUTO_RE = re.compile(r"\b(\d{8,14})([A-Z]{2})\b")
LEI_RE  = re.compile(r"LEI\s+\d+[\./]\d+", re.IGNORECASE)

# --- Campos extraídos para o assuntos.csv ------------------------------------
PROTOCOLO_RE = re.compile(r"31\.\d{6,10}(?:[_\-/]?\d{4})-\d{2}")
ASSUNTO_RE = re.compile(
    r"Assunto\s*[:\-]\s*"
    r"(.+?)"
    r"(?=\n[ \t]*\n"
    r"|\n[ \t]*(?:CPF|CNPJ|LOCAL|Relat[oó]rio|Data\b|Ref\.\s|Senhor|RELAT)"
    r"|\Z)",
    re.IGNORECASE | re.DOTALL,
)
# Ato/fato constitutivo: extraído do PDF do AUTO (SIF), NÃO do voto. No auto o
# rótulo divide a linha com outras colunas (AI) ou fica sozinho (AN); o valor vem
# nas linhas seguintes e termina em "DESCRIÇÃO COMPLEMENTAR".
ATO_RE = re.compile(
    r"ATO\s+OU\s+FATO\s+CONSTITUTIVO\s+DA\s+INFRA\w*[^\n]*\n"
    r"(.+?)"
    r"(?=\n[ \t]*(?:DESCRI[ÇC][ÃA]O\s+COMPLEMENTAR|DISPOSITIVO\s+LEGAL|MEDIDA\s+AFERIDA|ENQUADRAMENTO)"
    r"|\Z)",
    re.IGNORECASE | re.DOTALL,
)

_SENTINEL = object()   # sinaliza fim de fila para os workers

# Máximo de tentativas em falhas transitórias de rede (ver Tuning.MAX_RETRIES);
# sobrescrito em main() pelo --max-retries. 0 = infinito.
_RETRY_MAX = Tuning.MAX_RETRIES


# =============================================================================
# Cache SQLite
# =============================================================================

class CacheDB:
    """
    Caches persistentes em SQLite:
      - arquivos: índice do Drive (alimenta retomadas e evita duplicatas)
      - index_folders: pastas-raiz já indexadas com sucesso
      - meta: marcadores globais, como índice completo
      - sif_pares: pares (lei, ato) por voto
      - public_updates: arquivos novos/alterados ainda pendentes no índice público
      - drive_revisions: modifiedTime já concluído, para deduplicar a sobreposição
    Thread-safe via lock de escrita; leituras usam conexão por thread.
    """

    def __init__(self, path: Path):
        self._path = str(path)
        self._wlock = threading.Lock()
        self._local = threading.local()
        with self._conn() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS arquivos (
                    file_id     TEXT PRIMARY KEY,
                    file_name   TEXT NOT NULL,
                    file_size   TEXT,
                    destination TEXT NOT NULL,
                    instancia   TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_folders (
                    folder_id   TEXT PRIMARY KEY,
                    folder_name TEXT NOT NULL,
                    instancia   TEXT NOT NULL,
                    indexed_at  REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key         TEXT PRIMARY KEY,
                    value       TEXT
                );
                CREATE TABLE IF NOT EXISTS sif_pares (
                    file_id     TEXT PRIMARY KEY,
                    pares       TEXT   -- JSON list de [lei, ato]; NULL = sem par
                );
                CREATE TABLE IF NOT EXISTS public_updates (
                    file_id       TEXT PRIMARY KEY,
                    reason        TEXT NOT NULL,
                    modified_time TEXT,
                    detected_at   REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drive_revisions (
                    file_id       TEXT PRIMARY KEY,
                    modified_time TEXT NOT NULL,
                    processed_at  REAL NOT NULL
                );
            """)

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "con", None):
            self._local.con = sqlite3.connect(self._path, check_same_thread=False)
            self._local.con.execute("PRAGMA journal_mode=WAL")
        return self._local.con

    # --- índice ---

    def save_arquivo(self, item: "DownloadItem") -> None:
        with self._wlock:
            self._conn().execute(
                "INSERT OR IGNORE INTO arquivos VALUES (?,?,?,?,?)",
                (item.file_id, item.file_name, item.file_size,
                 str(item.destination), item.instancia),
            )
            self._conn().commit()

    def upsert_arquivo(self, item: "DownloadItem") -> None:
        """Inclui ou atualiza metadados sem criar uma segunda cópia do file_id."""
        with self._wlock:
            self._conn().execute(
                """
                INSERT INTO arquivos
                    (file_id, file_name, file_size, destination, instancia)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    file_name=excluded.file_name,
                    file_size=excluded.file_size,
                    destination=excluded.destination,
                    instancia=excluded.instancia
                """,
                (item.file_id, item.file_name, item.file_size,
                 str(item.destination), item.instancia),
            )
            self._conn().commit()

    def load_arquivos(self) -> list["DownloadItem"]:
        rows = self._conn().execute(
            "SELECT file_id, file_name, file_size, destination, instancia FROM arquivos"
        ).fetchall()
        return [DownloadItem(r[0], r[1], r[2], Path(r[3]), r[4]) for r in rows]

    def clear_arquivos(self) -> None:
        with self._wlock:
            self._conn().execute("DELETE FROM arquivos")
            self._conn().execute("DELETE FROM index_folders")
            self._conn().execute("DELETE FROM meta WHERE key='index_complete'")
            self._conn().commit()

    def is_index_complete(self) -> bool:
        row = self._conn().execute(
            "SELECT value FROM meta WHERE key='index_complete'"
        ).fetchone()
        return bool(row and row[0] == "1")

    def mark_index_complete(self, complete: bool = True) -> None:
        with self._wlock:
            self._conn().execute(
                "INSERT OR REPLACE INTO meta VALUES ('index_complete', ?)",
                ("1" if complete else "0",),
            )
            self._conn().commit()

    def mark_folder_indexed(self, folder: dict, instancia: str) -> None:
        with self._wlock:
            self._conn().execute(
                "INSERT OR REPLACE INTO index_folders VALUES (?,?,?,?)",
                (folder["id"], folder.get("name", folder["id"]), instancia, time.time()),
            )
            self._conn().commit()

    def indexed_folder_ids(self) -> set[str]:
        rows = self._conn().execute("SELECT folder_id FROM index_folders").fetchall()
        return {r[0] for r in rows}

    def count_indexed_folders(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM index_folders").fetchone()[0]

    def indexed_folders(self) -> list[dict[str, str]]:
        rows = self._conn().execute(
            "SELECT folder_id, folder_name, instancia FROM index_folders"
        ).fetchall()
        return [
            {"id": row[0], "name": row[1], "instancia": row[2]}
            for row in rows
        ]

    def get_meta(self, key: str) -> str | None:
        row = self._conn().execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._wlock:
            self._conn().execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )
            self._conn().commit()

    def mark_public_update(
        self, file_id: str, reason: str, modified_time: str | None = None
    ) -> None:
        with self._wlock:
            self._conn().execute(
                """
                INSERT INTO public_updates
                    (file_id, reason, modified_time, detected_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    reason=excluded.reason,
                    modified_time=excluded.modified_time,
                    detected_at=excluded.detected_at
                """,
                (file_id, reason, modified_time, time.time()),
            )
            self._conn().commit()

    def pending_public_updates(self) -> dict[str, str]:
        rows = self._conn().execute(
            "SELECT file_id, reason FROM public_updates"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def get_drive_revision(self, file_id: str) -> str | None:
        row = self._conn().execute(
            "SELECT modified_time FROM drive_revisions WHERE file_id=?",
            (file_id,),
        ).fetchone()
        return row[0] if row else None

    def clear_public_update(self, file_id: str) -> None:
        with self._wlock:
            con = self._conn()
            row = con.execute(
                "SELECT modified_time FROM public_updates WHERE file_id=?",
                (file_id,),
            ).fetchone()
            if row and row[0]:
                con.execute(
                    """
                    INSERT INTO drive_revisions
                        (file_id, modified_time, processed_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(file_id) DO UPDATE SET
                        modified_time=excluded.modified_time,
                        processed_at=excluded.processed_at
                    """,
                    (file_id, row[0], time.time()),
                )
            con.execute("DELETE FROM public_updates WHERE file_id=?", (file_id,))
            self._conn().commit()

    def clear_sif(self) -> None:
        with self._wlock:
            self._conn().execute("DELETE FROM sif_pares")
            self._conn().commit()

    # --- pares (lei, ato) por voto, para a estrutura LEI/ATO ---

    def get_pares(self, file_id: str) -> list[tuple[str, str]] | None:
        """Lista de (lei, ato) do voto. [] = processado sem par; None = não processado."""
        row = self._conn().execute(
            "SELECT pares FROM sif_pares WHERE file_id=?", (file_id,)
        ).fetchone()
        if row is None:
            return None
        import json
        return [tuple(p) for p in json.loads(row[0])] if row[0] else []

    def save_pares(self, file_id: str, pares: list[tuple[str, str]]) -> None:
        import json
        with self._wlock:
            self._conn().execute(
                "INSERT OR REPLACE INTO sif_pares VALUES (?,?)",
                (file_id, json.dumps(pares) if pares else None),
            )
            self._conn().commit()


# =============================================================================
# Filtros de pasta
# =============================================================================

def _norm(v: str) -> str:
    d = unicodedata.normalize("NFKD", v)
    return re.sub(r"\s+", " ", "".join(c for c in d if not unicodedata.combining(c)).casefold()).strip()

_2A = {_norm(n) for n in {
    "voto dos relatores","votos dos relatores","voto de relatores","votos de relatores",
    "voto relatores","votos relatores","voto dos relatores(as)","votos dos relatores(as)",
    "voto dos relatres","votos dos relatres","voto das relatoras","votos das relatoras","votos relatoras",
}}

def is_2a(name: str) -> bool:
    n = _norm(name)
    return n in _2A or (bool(re.search(r"\bvotos?\b", n)) and any(
        t in n for t in ("relator","relatores","relatora","relatoras","relatres")))

_1A_RE = re.compile(r"^sessao[^\d]+(\d{3})\b")
def is_1a(name: str) -> bool:
    m = _1A_RE.match(_norm(name))
    # As sessões continuam crescendo; limitar a 449 fazia as novas deixarem de
    # aparecer silenciosamente no acervo.
    return bool(m) and 1 <= int(m.group(1)) <= 999


# =============================================================================
# Dataclasses
# =============================================================================

@dataclass
class DownloadItem:
    file_id:   str
    file_name: str
    file_size: str | None
    destination: Path
    instancia: str


@dataclass
class Stats:
    _lock:       threading.Lock  = field(default_factory=threading.Lock)
    total:       int = 0          # cresce conforme indexação descobre arquivos
    indexed:     int = 0          # pastas-raiz concluídas
    downloaded:  int = 0
    skipped:     int = 0
    pulados:      int = 0   # --continuar: já feitos, pulados sem baixar
    organized:    int = 0
    sem_ato:      int = 0
    autos_total:  int = 0   # autos encontrados nos votos
    autos_done:   int = 0   # autos consultados no SIF (cache hit ou query nova)
    errors:       list[str] = field(default_factory=list)
    ato_counter:  Counter   = field(default_factory=Counter)

    def inc(self, attr: str, by: int = 1):
        with self._lock:
            setattr(self, attr, getattr(self, attr) + by)

    def add(self, attr: str, by: int = 1):
        self.inc(attr, by)

    def add_error(self, msg: str):
        with self._lock:
            self.errors.append(msg)

    def add_ato(self, ato: str, instancia: str):
        with self._lock:
            self.ato_counter[f"{instancia}/{ato}"] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "total":       self.total,
                "indexed":     self.indexed,
                "downloaded":  self.downloaded,
                "skipped":     self.skipped,
                "pulados":     self.pulados,
                "organized":   self.organized,
                "sem_ato":     self.sem_ato,
                "autos_total": self.autos_total,
                "autos_done":  self.autos_done,
                "errors":      len(self.errors),
                "ato":         list(self.ato_counter.most_common(12)),
            }


# =============================================================================
# Utilitários
# =============================================================================

def _san(v: str, fb: str = "sem_nome") -> str:
    v = unicodedata.normalize("NFC", v).strip()
    v = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", v)
    v = re.sub(r"\s+", " ", v).strip(" .")
    return v or fb

def _san_dir(v: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", v)

def _token_palavra(palavra: str) -> str:
    """Normaliza palavra final ignorando pontuação: 'da,' -> 'DA'."""
    return "".join(c for c in palavra.upper() if c.isalnum())


def _palavra_final_solta(palavra: str) -> bool:
    return _token_palavra(palavra) in Tuning.PALAVRAS_FINAIS_SOLTAS

# Lixo que o SIF às vezes mistura no "ato": valores numéricos de colunas vizinhas
# (MEDIDA AFERIDA / BASE PARA CÁLCULO) e o próprio rótulo "AFERIDA". Removidos do
# nome da pasta para não fragmentar atos iguais em pastas diferentes.
_AFERIDA_RE = re.compile(r"\bAFERIDA\w*", re.IGNORECASE)
# número (inclui separadores internos . , como em 31.361,93) mas NÃO engole a
# pontuação final: tem que terminar em dígito, senão a vírgula real some.
_NUM_RE     = re.compile(r"\d[\d.,]*\d|\d")
_ESP_PONT_RE = re.compile(r"\s+([,.;:])")   # "X ," -> "X,"

def _limpar_ato(nome: str) -> str:
    nome = _AFERIDA_RE.sub(" ", nome)
    nome = _NUM_RE.sub(" ", nome)
    nome = _ESP_PONT_RE.sub(r"\1", nome)
    return " ".join(nome.split())

def nome_pasta_do_ato(ato: str, max_palavras: int = Tuning.MAX_PALAVRAS_ATO) -> str:
    """
    Nome da pasta a partir do ato/fato constitutivo. PRIMEIRO remove o lixo que o
    SIF mistura no ato — números (valores de MEDIDA AFERIDA / BASE PARA CÁLCULO) e
    a palavra "AFERIDA" — e SÓ DEPOIS corta as primeiras `max_palavras` palavras.
    Limpar antes do corte é essencial: senão o lixo ocupa vagas de palavra e o
    mesmo ato fragmenta (p.ex. "...MEDIDAS 999 MITIGADORAS" cortaria diferente de
    "...MEDIDAS AFERIDA 999", que perderia o "MITIGADORAS"). Depois do corte,
    remove conectivos finais soltos, como "DA", "E", "OU" ou "COM". Tira
    vírgula/underscore final e limita o comprimento.
    """
    # .upper() unifica a mesma frase com acentos em caixas diferentes
    # (p.ex. "EDIFICaçãO" vs "EDIFICAÇÃO") — causa nº 1 de pastas duplicadas.
    palavras = _limpar_ato(_san_dir(ato.upper())).split()
    if not palavras:
        return ""
    n = min(max_palavras, len(palavras))
    while n > 1 and _palavra_final_solta(palavras[n - 1]):
        n -= 1
    nome = " ".join(palavras[:n]).strip(" .,_")
    return nome[:120].rstrip(" .,_") if len(nome) > 120 else nome

def _eq(v: str) -> str:
    return v.replace("\\","\\\\").replace("'","\\'")

def _skip(path: Path, size: str | None) -> bool:
    if not path.exists() or not size:
        return False
    try:
        return path.stat().st_size == int(size)
    except (OSError, ValueError):
        return False

def _iter(service, query: str, fields: str) -> Iterable[dict]:
    token = None
    while True:
        r = _retry(
            lambda: service.files().list(
                q=query, spaces="drive",
                fields=f"nextPageToken,files({fields})",
                pageSize=1000, pageToken=token,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute(),
            what="Drive list",
        )
        yield from r.get("files", [])
        token = r.get("nextPageToken")
        if not token:
            break

def _resolve(item: dict) -> dict:
    d = item.get("shortcutDetails") or {}
    tid, tmime = d.get("targetId"), d.get("targetMimeType")
    if not tid or not tmime:
        return item
    return {**item, "id": tid, "mimeType": tmime, "name": item.get("name") or tid}

def _unique(path: Path, seen: set[Path]) -> Path:
    if not path.exists() and path not in seen:
        return path
    for i in range(1, 10_000):
        c = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not c.exists() and c not in seen:
            return c
    raise RuntimeError(f"Sem nome único: {path}")


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_datetime(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _eta(stats_snapshot: dict, total_folders: int, done_count: int,
         start_time: float, now: float) -> tuple[str, str]:
    elapsed = max(now - start_time, 0.0)
    rate = done_count / elapsed if elapsed > 0 and done_count > 0 else 0.0
    if rate <= 0:
        return "calculando", "sem taxa ainda"

    total_items = stats_snapshot["total"]
    estimate_kind = "atual"
    indexed = stats_snapshot["indexed"]
    if total_folders and 0 < indexed < total_folders:
        # Enquanto a indexação cresce, o total descoberto ainda é parcial.
        total_items = max(total_items, int(total_items / indexed * total_folders))
        estimate_kind = "estimada"

    remaining = max(total_items - done_count, 0)
    eta_seconds = remaining / rate
    return _fmt_datetime(now + eta_seconds), (
        f"{_fmt_duration(eta_seconds)} restantes, {estimate_kind}, "
        f"{rate:.1f} arquivos/s"
    )


def _retry(fn, *, what: str, base: float = Tuning.RETRY_BACKOFF, cap: float = Tuning.RETRY_BACKOFF_CAP):
    """
    Executa fn() com backoff exponencial (dorme entre tentativas, sem busy-loop).
    Após _RETRY_MAX tentativas, levanta a exceção para quem chamou tratar (loga e
    segue) — não trava nem aborta a run. _RETRY_MAX=0 = infinito. Use só em falhas
    transitórias de rede; erros permanentes (arquivo corrompido etc.) devem ser
    tratados por quem chama, sem passar por aqui.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return fn()
        except Exception as exc:
            if _RETRY_MAX and attempt >= _RETRY_MAX:
                raise
            delay = min(base * (2 ** (attempt - 1)), cap)
            print(f"[retry] {what}: tentativa {attempt} falhou ({exc!r}); "
                  f"pausando {delay:.0f}s e tentando de novo", flush=True)
            time.sleep(delay)


def _extract_protocolo(file_name: str, text: str) -> str:
    m = PROTOCOLO_RE.search(file_name) or PROTOCOLO_RE.search(text)
    return m.group(0) if m else ""


def _extract_assunto(text: str) -> str:
    m = ASSUNTO_RE.search(text)
    return " ".join(m.group(1).split()) if m else ""


def _extract_ato(text: str) -> str:
    """Extrai o ato/fato constitutivo do texto do PDF do AUTO (não do voto)."""
    m = ATO_RE.search(text)
    if not m:
        return ""
    val = " ".join(m.group(1).split())
    # remove número solto no meio (lixo de coluna vizinha "MEDIDA AFERIDA" nos AI)
    return re.sub(r"\s+\d+\s+", " ", val).strip()


# --- Leitura do voto conforme o formato (a única diferença PDF vs Word) -------

def _soffice_cmd() -> list[str] | None:
    """Comando do LibreOffice (nativo no PATH ou via flatpak), p/ converter .doc."""
    for c in ("soffice", "libreoffice"):
        p = shutil.which(c)
        if p:
            return [p]
    if shutil.which("flatpak"):
        return ["flatpak", "run", "org.libreoffice.LibreOffice"]
    direct = "/var/lib/flatpak/exports/bin/org.libreoffice.LibreOffice"
    if Path(direct).exists():
        return [direct]
    return None

_SOFFICE = _soffice_cmd()
_DOC_SEM = threading.Semaphore(Tuning.DOC_CONVERT_CONCURRENCY)


def _texto_doc(path: Path) -> str | None:
    """Converte .doc -> txt via LibreOffice (best-effort). Falha => None."""
    if not _SOFFICE:
        return None
    with _DOC_SEM:
        # Temp sob o cwd (na home, acessível ao flatpak; /tmp pode ficar fora do
        # sandbox). Perfil próprio permite instâncias paralelas do soffice.
        tmp = Path(tempfile.mkdtemp(prefix=".lo_", dir=Path.cwd()))
        try:
            prof, out = tmp / "profile", tmp / "out"
            out.mkdir()
            subprocess.run(
                _SOFFICE + ["--headless", f"-env:UserInstallation=file://{prof}",
                            "--convert-to", "txt:Text", "--outdir", str(out), str(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=180, check=False,
            )
            txts = list(out.glob("*.txt"))
            return txts[0].read_text(encoding="utf-8", errors="ignore") if txts else None
        except Exception:
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _extrair_texto(item: "DownloadItem") -> str | None:
    """Texto do voto conforme o tipo (pela extensão). None => não deu pra ler."""
    ext = item.destination.suffix.lower()
    try:
        if ext == ".pdf":
            with pdfplumber.open(item.destination) as pdf:
                return "\n".join(p.extract_text() or "" for p in pdf.pages[:3])
        if ext == ".txt":   # Google Docs exportado para texto
            return item.destination.read_text(encoding="utf-8", errors="ignore")
        if ext == ".docx":
            return None if docx2txt is None else (docx2txt.process(str(item.destination)) or "")
        if ext == ".doc":
            return _texto_doc(item.destination)
    except Exception:
        return None
    return None


def _fname_para(cname: str, mime: str) -> str:
    """Nome local do arquivo com a extensão certa para o tipo."""
    ext = MIME_EXT.get(mime, "")
    return cname if cname.casefold().endswith(ext) else f"{cname}{ext}"


class CsvWriter:
    """Grava o assuntos.csv de forma thread-safe (vários organize workers)."""

    def __init__(self, path: Path, append: bool = True):
        self._lock = threading.Lock()
        self._seen: set[tuple[str, str, str]] = set()
        self.rows = 0

        if append and path.exists() and path.stat().st_size > 0:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                reader = csv.reader(fh)
                header = next(reader, None)
                if header == ["protocolo", "assunto", "ato_constitutivo"]:
                    for row in reader:
                        if len(row) >= 3:
                            key = (row[0], row[1], row[2])
                            self._seen.add(key)
                    self.rows = len(self._seen)
                else:
                    append = False

        mode = "a" if append and path.exists() and path.stat().st_size > 0 else "w"
        self._fh = path.open(mode, newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._fh)
        if mode == "w":
            self._writer.writerow(["protocolo", "assunto", "ato_constitutivo"])
            self._fh.flush()

    def write_row(self, protocolo: str, assunto: str, ato: str) -> None:
        key = (protocolo, assunto, ato)
        with self._lock:
            if key in self._seen:
                return
            self._seen.add(key)
            self._writer.writerow([protocolo, assunto, ato])
            self._fh.flush()
            self.rows += 1

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.close()
            except Exception:
                pass


# =============================================================================
# Autenticação
# =============================================================================

def _load_creds(creds_path: Path, token_path: Path) -> Credentials:
    if not creds_path.exists():
        raise FileNotFoundError(f"{creds_path} não encontrado.")
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds

def _new_service(token_path: Path):
    """Service Drive independente por thread."""
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    return build("drive", "v3", credentials=creds)


# =============================================================================
# Estágio 1 — Index worker
# Cada worker recebe uma pasta-raiz, lista seus arquivos recursivamente
# e os coloca diretamente na download_queue.
# =============================================================================

def index_worker(
    token_path:    Path,
    folder:        dict,
    folder_dest:   Path,
    instancia:     str,
    allowed_mimes: set[str],
    download_queue: "queue.Queue",
    seen_files:    set[str],
    seen_lock:     threading.Lock,
    planned:       set[Path],
    planned_lock:  threading.Lock,
    stats:         Stats,
    db:            "CacheDB | None" = None,
) -> None:
    service = _new_service(token_path)
    ok = _recurse(service, folder["id"], folder_dest, instancia, allowed_mimes,
                  seen_files, seen_lock, planned, planned_lock,
                  stats, download_queue, db)
    if ok:
        if db:
            db.mark_folder_indexed(folder, instancia)
        stats.inc("indexed")
    else:
        stats.add_error(f"Index incompleto: {folder.get('name', folder['id'])}")


def _recurse(
    service, folder_id: str, local_dir: Path, instancia: str, allowed_mimes: set[str],
    seen_files: set[str], seen_lock: threading.Lock,
    planned: set[Path], planned_lock: threading.Lock,
    stats: Stats, download_queue: "queue.Queue",
    db: "CacheDB | None" = None,
) -> bool:
    try:
        children = list(_iter(
            service,
            f"('{_eq(folder_id)}' in parents) and (trashed=false)",
            "id,name,mimeType,size,shortcutDetails",
        ))
    except HttpError as exc:
        stats.add_error(f"Listar {folder_id}: {exc}")
        return False

    ok = True
    for raw in children:
        c = _resolve(raw)
        cid  = c.get("id")
        if not cid:
            continue
        cname = _san(c.get("name", cid))
        cmime = c.get("mimeType")

        if cmime == FOLDER_MIME:
            child_ok = _recurse(service, cid, local_dir / cname, instancia, allowed_mimes,
                                seen_files, seen_lock, planned, planned_lock,
                                stats, download_queue, db)
            ok = ok and child_ok
            continue

        if cmime not in allowed_mimes:
            continue

        with seen_lock:
            if cid in seen_files:
                continue
            seen_files.add(cid)

        fname = _fname_para(cname, cmime)
        dest  = local_dir / fname

        if _skip(dest, c.get("size")):
            stats.inc("total")
            stats.inc("skipped")
            item = DownloadItem(cid, fname, c.get("size"), dest, instancia)
            if db:
                db.save_arquivo(item)
            download_queue.put(item)
            continue

        with planned_lock:
            dest = _unique(dest, planned)
            planned.add(dest)

        stats.inc("total")
        item = DownloadItem(cid, fname, c.get("size"), dest, instancia)
        if db:
            db.save_arquivo(item)
        download_queue.put(item)

    return ok


# =============================================================================
# Estágio 2 — Download worker
# Consome download_queue, baixa o arquivo, empurra para organize_queue.
# =============================================================================

def download_worker(
    token_path:      Path,
    download_queue:  "queue.Queue",
    organize_queue:  "queue.Queue",
    stats:           Stats,
    skip_done=None,
) -> None:
    service = _new_service(token_path)
    while True:
        item = download_queue.get()
        if item is _SENTINEL:
            download_queue.put(_SENTINEL)   # propaga para outros workers
            return
        try:
            if skip_done is not None and skip_done(item):
                stats.inc("pulados")        # --continuar: já feito, não re-baixa
                continue
            if not _skip(item.destination, item.file_size):
                _retry(lambda: _do_download(service, item),
                       what=f"Download {item.file_name}")
                stats.inc("downloaded")
            organize_queue.put(item)
        except Exception as exc:
            stats.add_error(f"Download {item.file_name}: {exc}")


def _do_download(service, item: DownloadItem) -> None:
    dest = item.destination
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.suffix.lower() == ".txt":   # Google Docs nativo: exporta p/ texto
        req = service.files().export_media(fileId=item.file_id, mimeType="text/plain")
    else:
        req = service.files().get_media(fileId=item.file_id, supportsAllDrives=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with tmp.open("wb") as fh:
        dl = MediaIoBaseDownload(fh, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
    tmp.replace(dest)


# =============================================================================
# Estágio 3 — Organize worker
# Consome organize_queue, extrai autos, consulta SIF, copia para pasta do ato.
# =============================================================================

class SifClient:
    """Rate-limit global; HTTP+parse em paralelo (sem lock no I/O)."""

    def __init__(self, rate: float = Tuning.SIF_RATE):
        self._session:      "requests.Session | None" = None
        self._sess_lock     = threading.Lock()
        self._cache:        dict[str, tuple[str | None, str | None]] = {}
        self._cache_lock    = threading.Lock()
        self._throttle_lock = threading.Lock()
        self._interval      = 1.0 / max(rate, 0.1)
        self._last          = 0.0

    def _session_(self) -> "requests.Session":
        with self._sess_lock:
            if self._session is None:
                s = requests.Session()
                s.headers["User-Agent"] = "Mozilla/5.0"
                try:
                    s.post(LOGIN_URL, data={"login": LOGIN_USER, "password": LOGIN_PASS,
                                            "Button_DoLogin": "Login"}, timeout=30).raise_for_status()
                except Exception as e:
                    print(f"[SIF] login: {e}", flush=True)
                self._session = s
            return self._session

    def _throttle(self):
        with self._throttle_lock:
            wait = self._interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def consultar_auto(self, idn: str, tipo: str) -> tuple[str | None, str | None]:
        """Retorna (lei, ato) do PDF do auto. Uma só busca extrai os dois."""
        key = f"{idn}{tipo}"
        with self._cache_lock:
            if key in self._cache:
                return self._cache[key]

        def attempt():
            self._throttle()
            return self._query(self._session_(), idn, tipo)

        result = _retry(attempt, what=f"SIF {idn}{tipo}")
        with self._cache_lock:
            self._cache[key] = result
        return result

    def _query(self, s: "requests.Session", idn: str, tipo: str) -> tuple[str | None, str | None]:
        # Falha de rede/HTTP sobe para o _retry (pausa e tenta de novo).
        r = s.get(AUTO_URL, params={"Idn_Doct_Lavr": idn, "Tip_Auto": tipo}, timeout=30)
        r.raise_for_status()
        # Resposta sem PDF = auto não encontrado: sem lei / sem ato (legítimo).
        idx = r.content.find(b"%PDF")
        if idx == -1:
            return None, None
        try:
            with pdfplumber.open(io.BytesIO(r.content[idx:])) as pdf:
                txt = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception:
            return None, None
        # Lei (DISPOSITIVO LEGAL TRANSGREDIDO)
        lei = None
        pos = txt.upper().find("DISPOSITIVO LEGAL TRANSGREDIDO")
        if pos != -1:
            m = LEI_RE.search(txt[pos: pos + 400])
            if m:
                lei = re.sub(r"\s+", " ", m.group(0).upper()).strip()
        # Ato/fato constitutivo da infração (do mesmo PDF do auto)
        ato = _extract_ato(txt) or None
        return lei, ato


def organize_worker(
    organize_queue: "queue.Queue",
    sif:            SifClient,
    ato_dir:        Path,
    stats:          Stats,
    csv_writer:     "CsvWriter | None" = None,
    db:             "CacheDB | None" = None,
) -> None:
    while True:
        item = organize_queue.get()
        if item is _SENTINEL:
            organize_queue.put(_SENTINEL)
            return
        try:
            _do_organize(item, sif, ato_dir, stats, csv_writer, db)
        except Exception as exc:
            stats.add_error(f"Organize {item.file_name}: {exc}")


_DECISAO_RE = re.compile(r"DISPOSITIVO DA DECIS[AÃ]O", re.IGNORECASE)
_ATA_SESSAO_TEXTO_RE = re.compile(r"\bata\s+da\b.{0,180}\bsessao\b", re.IGNORECASE)
_ATA_NOME_RE = re.compile(r"^ata(?:\b|\s|\d)", re.IGNORECASE)
_CONTROLE_NOME_RE = re.compile(r"^controle\s+de\s+votacao\b", re.IGNORECASE)
_SESSAO_NOME_RE = re.compile(r"^sessao\s*\d", re.IGNORECASE)


def _nome_sessao_sem_prefixo(nome: str) -> str:
    nome_n = _norm(Path(nome).name)
    nome_n = re.sub(r"^[^0-9a-z]+", "", nome_n)
    return re.sub(r"^copia\s+de\s+", "", nome_n)


def motivo_documento_de_sessao_nao_decisao(nome: str, texto: str) -> str:
    """
    Identifica documentos de sessão que não são voto individual. ATAs e controles
    de votação costumam ter vários "Dispositivo da decisão" no corpo, então o
    filtro de decisão sozinho não basta; eles precisam sair antes de consultar o
    SIF e antes de copiar para a saída final.
    """
    nome_n = _nome_sessao_sem_prefixo(nome)
    texto_n = _norm(texto[:20_000])
    if not texto_n:
        return ""

    if "controle de votacao" in texto_n:
        return "controle de votacao"

    if _ATA_SESSAO_TEXTO_RE.search(texto_n) or "ata da sessao" in texto_n:
        return "ata de sessao"

    contexto_sessao = (
        "junta integrada de julgamento fiscal" in texto_n
        and "sessao" in texto_n
        and (
            "membros presentes" in texto_n
            or "foi aberta a sessao" in texto_n
            or "presidente" in texto_n
            or "secretaria" in texto_n
        )
    )

    if "defesas julgadas" in texto_n and contexto_sessao:
        return "ata com defesas julgadas"

    nome_forte = (
        bool(_ATA_NOME_RE.search(nome_n))
        or bool(_CONTROLE_NOME_RE.search(nome_n))
        or bool(_SESSAO_NOME_RE.search(nome_n))
    )
    if nome_forte and contexto_sessao:
        return "nome de sessao confirmado pelo conteudo"

    return ""


def eh_documento_de_sessao_nao_decisao(nome: str, texto: str) -> bool:
    return bool(motivo_documento_de_sessao_nao_decisao(nome, texto))


def _do_organize(item: DownloadItem, sif: SifClient, ato_dir: Path, stats: Stats,
                 csv_writer: "CsvWriter | None" = None,
                 db: "CacheDB | None" = None) -> None:
    try:
        _organizar(item, sif, ato_dir, stats, csv_writer, db)
    finally:
        # Deleta o bruto após processar, independente do resultado
        try:
            item.destination.unlink(missing_ok=True)
        except Exception:
            pass


def _nome_lei(lei: str | None) -> str:
    return _san_dir(lei).strip() if lei else "SEM LEI"


def _copiar_para_lei_ato(item: DownloadItem, pares: Iterable[tuple], out_dir: Path, stats: Stats) -> None:
    # Estrutura LEI/ATO: out_dir/instancia/<LEI>/<ATO>/voto. Dedup por (lei, ato)
    # já encurtados — pares distintos podem cair no mesmo destino.
    destinos = sorted({(_nome_lei(lei), nome_pasta_do_ato(ato))
                       for lei, ato in pares if ato})
    for nome_lei, nome_ato in destinos:
        if not nome_ato:
            continue
        d = out_dir / item.instancia / nome_lei / nome_ato
        d.mkdir(parents=True, exist_ok=True)
        dst = d / item.destination.name
        if not dst.exists():
            shutil.copy2(item.destination, dst)
        stats.inc("organized")
        stats.add_ato(f"{nome_lei}/{nome_ato}", item.instancia)


def _escrever_csv(csv_writer: "CsvWriter | None", protocolo: str, assunto: str,
                  atos: list[str]) -> None:
    if csv_writer is None:
        return
    ato_cell = " | ".join(atos)   # atos distintos do(s) auto(s), 1 linha por voto
    if protocolo or assunto or ato_cell:
        csv_writer.write_row(protocolo, assunto, ato_cell)


def _atos_distintos(pares: Iterable[tuple]) -> list[str]:
    """Atos distintos (na ordem) a partir dos pares (lei, ato) — para o CSV."""
    atos: list[str] = []
    for _lei, ato in pares:
        if ato and ato not in atos:
            atos.append(ato)
    return atos


def _organizar(item: DownloadItem, sif: SifClient, ato_dir: Path, stats: Stats,
               csv_writer: "CsvWriter | None" = None,
               db: "CacheDB | None" = None) -> None:
    # Lê o voto conforme o formato (PDF/Word/Docs) — filtro de decisão + dados.
    txt = _extrair_texto(item)
    if txt is None:
        stats.inc("sem_ato")
        if db:
            db.save_pares(item.file_id, [])
        return

    motivo_nao_decisao = motivo_documento_de_sessao_nao_decisao(item.file_name, txt)
    if motivo_nao_decisao:
        stats.inc("sem_ato")
        if db:
            db.save_pares(item.file_id, [])
        print(f"[nao-decisao] {motivo_nao_decisao}: {item.file_name}", flush=True)
        return

    # Descarta imediatamente se não for decisão
    if not _DECISAO_RE.search(txt):
        stats.inc("sem_ato")
        if db:
            db.save_pares(item.file_id, [])
        return

    protocolo = _extract_protocolo(item.file_name, txt)
    assunto   = _extract_assunto(txt)

    # Pares (lei, ato) por voto: do cache ou consultando o SIF (por auto). Cada
    # auto dá um par lei↔ato, que vira a estrutura LEI/ATO. O ato vem do PDF do AUTO.
    if db:
        pares = db.get_pares(item.file_id)
        if pares is not None:
            _escrever_csv(csv_writer, protocolo, assunto, _atos_distintos(pares))
            if not pares:
                stats.inc("sem_ato")
            else:
                _copiar_para_lei_ato(item, pares, ato_dir, stats)
            return

    autos = AUTO_RE.findall(txt)
    if not autos:
        stats.inc("sem_ato")
        if db:
            db.save_pares(item.file_id, [])
        _escrever_csv(csv_writer, protocolo, assunto, [])
        return

    stats.inc("autos_total", len(autos))
    pares: list[tuple[str, str]] = []
    for idn, tipo in autos:
        lei, ato = sif.consultar_auto(idn, tipo)
        stats.inc("autos_done")
        if ato:                       # só organiza autos que têm ato
            pares.append((lei or "", ato))

    atos = _atos_distintos(pares)
    if db:
        db.save_pares(item.file_id, pares)

    _escrever_csv(csv_writer, protocolo, assunto, atos)

    if not pares:
        stats.inc("sem_ato")
        return

    _copiar_para_lei_ato(item, pares, ato_dir, stats)


# =============================================================================
# Display
# =============================================================================

def _display(
    stats:          Stats,
    total_folders:  int,
    download_q:     "queue.Queue",
    organize_q:     "queue.Queue",
    start_time:      float,
    stop:           threading.Event,
) -> None:
    while not stop.wait(3):
        s    = stats.snapshot()
        now  = time.time()
        ts   = time.strftime("%H:%M:%S", time.localtime(now))

        # Progresso real = totalmente processados (organizados + sem_ato + pulados) / descobertos
        done_count = s["organized"] + s["sem_ato"] + s["pulados"]
        pct_done  = (done_count / s["total"] * 100) if s["total"] else 0

        # Progresso SIF = autos verificados / autos encontrados
        pct_sif   = (s["autos_done"] / s["autos_total"] * 100) if s["autos_total"] else 0
        eta_at, eta_desc = _eta(s, total_folders, done_count, start_time, now)

        lines = [
            "",
            f"---------- [{ts}] ----------",
            f"Início          : {_fmt_datetime(start_time)}",
            f"Transcorrido    : {_fmt_duration(now - start_time)}",
            f"Previsão fim    : {eta_at} ({eta_desc})",
            f"",
            f"Pastas indexadas : {s['indexed']:>4} / {total_folders}",
            f"Arquivos descob. : {s['total']}",
            f"Baixados         : {s['downloaded'] + s['skipped']:>6}  ({s['downloaded']} novos + {s['skipped']} já tinham)" + (f"  |  pulados: {s['pulados']}" if s['pulados'] else ""),
            f"",
            f"Progresso geral  : {done_count:>6} / {s['total']}  ({pct_done:.1f}%)",
            f"Autos verificados: {s['autos_done']:>6} / {s['autos_total']}  ({pct_sif:.1f}%)",
            f"",
            f"Fila download    : {download_q.qsize():>4}  |  Fila organizar: {organize_q.qsize():>4}",
            f"Organizados      : {s['organized']:>6}  |  Sem ato: {s['sem_ato']}  |  Erros: {s['errors']}",
        ]
        if s["ato"]:
            lines.append("")
            for key, cnt in sorted(s["ato"], key=lambda x: -x[1]):
                inst, ato = key.split("/", 1)
                lines.append(f"  {cnt:>5}  {'1ª' if '1a' in inst else '2ª'}  {ato[:50]}")
        print("\n".join(lines), flush=True)


# =============================================================================
# Main
# =============================================================================

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pipeline contínuo: indexa → baixa → organiza por ato (PDF + Word).")
    p.add_argument("--tipos",            default="pdf,gdoc,docx,doc",
                   help="Formatos a processar, separados por vírgula: pdf,gdoc,docx,doc (padrão: todos). "
                        "Para incluir Word num índice já feito só de PDF, rode com --refresh-index.")
    p.add_argument("--doc-concurrency",  type=int, default=Tuning.DOC_CONVERT_CONCURRENCY,
                   help=f"Conversões LibreOffice (.doc) simultâneas (padrão: {Tuning.DOC_CONVERT_CONCURRENCY})")
    p.add_argument("--output",           default="votos_relatores_pdfs")
    p.add_argument("--ato-output",       default="pdfs_por_lei_e_ato", help="Saída: <dir>/<instancia>/<LEI>/<ATO>/voto (padrão: pdfs_por_lei_e_ato)")
    p.add_argument("--credentials",      default="credentials.json")
    p.add_argument("--token",            default="token.json")
    p.add_argument("--cache",            default="pipeline_cache.db", help="Arquivo SQLite de cache (padrão: pipeline_cache.db)")
    p.add_argument("--instancia",        choices=["1","2","ambas"], default="ambas")
    p.add_argument("--index-workers",    type=int,   default=Tuning.INDEX_WORKERS,    help=f"Workers de indexação (padrão: {Tuning.INDEX_WORKERS})")
    p.add_argument("--download-workers", type=int,   default=Tuning.DOWNLOAD_WORKERS, help=f"Workers de download (padrão: {Tuning.DOWNLOAD_WORKERS})")
    p.add_argument("--organize-workers", type=int,   default=Tuning.ORGANIZE_WORKERS, help=f"Workers de organização (padrão: {Tuning.ORGANIZE_WORKERS})")
    p.add_argument("--sif-rate",         type=float, default=Tuning.SIF_RATE,         help=f"Consultas SIF/s (padrão: {Tuning.SIF_RATE})")
    p.add_argument("--csv-output",       default="assuntos.csv",  help="CSV com protocolo/assunto/ato das decisões (padrão: assuntos.csv)")
    p.add_argument("--no-csv",           action="store_true",     help="Não gera o assuntos.csv")
    p.add_argument("--fresh-csv",        action="store_true",     help="Recria o CSV do zero; por padrão, retoma em append sem duplicar linhas")
    p.add_argument("--max-retries",      type=int,   default=Tuning.MAX_RETRIES, help=f"Máx. tentativas em falhas de rede antes de desistir do item; 0 = infinito (padrão: {Tuning.MAX_RETRIES})")
    p.add_argument("--so-download",      action="store_true",     help="Só baixa, sem organizar")
    p.add_argument("--refresh-index",    action="store_true",     help="Ignora cache de índice e reindexe o Drive")
    p.add_argument("--refresh-sif",      action="store_true",     help="Ignora cache SIF e reconsulta todos os autos")
    p.add_argument("--no-cache",         action="store_true",     help="Desativa o cache completamente")
    p.add_argument("--continuar",        action="store_true",     help="Retoma sem re-baixar tudo: pula votos já organizados (arquivo já na saída por ato, ou sem par no cache) e baixa só o que falta")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    start_time = time.time()
    global _RETRY_MAX, _DOC_SEM
    _RETRY_MAX = max(0, args.max_retries)
    _DOC_SEM = threading.Semaphore(max(1, args.doc_concurrency))
    if not HAS_PDF_DEPS and not args.so_download:
        print("Instale: pip install pdfplumber requests\nOu use --so-download")

    tipos = [t.strip() for t in args.tipos.split(",") if t.strip()]
    invalidos = [t for t in tipos if t not in TIPO_MIME]
    if invalidos:
        print(f"Tipos inválidos: {invalidos}. Use: {', '.join(TIPO_MIME)}", file=sys.stderr)
        return 2
    allowed_mimes = {TIPO_MIME[t] for t in tipos}
    if "doc" in tipos and not _SOFFICE:
        print("Aviso: .doc pedido mas LibreOffice não encontrado — esses virão 'sem ato'.", flush=True)
    if "docx" in tipos and docx2txt is None:
        print("Aviso: .docx pedido mas docx2txt não instalado — esses virão 'sem ato'.", flush=True)
    print(f"Tipos: {', '.join(tipos)}", flush=True)

    base = Path.cwd()
    R = lambda s: Path(s) if Path(s).is_absolute() else base / s
    output_dir   = R(args.output)
    ato_dir      = R(args.ato_output)
    creds_path   = R(args.credentials)
    token_path   = R(args.token)
    instancias   = ["1","2"] if args.instancia == "ambas" else [args.instancia]

    # Cache SQLite
    db: CacheDB | None = None
    if not args.no_cache:
        db = CacheDB(R(args.cache))
        if args.refresh_index:
            db.clear_arquivos()
            print("Cache de índice limpo.", flush=True)
        if args.refresh_sif:
            db.clear_sif()
            print("Cache SIF limpo.", flush=True)

    # Auth interativa (uma vez, no thread principal)
    print("Autenticando...", flush=True)
    try:
        _load_creds(creds_path, token_path)
    except FileNotFoundError as e:
        print(e, file=sys.stderr); return 2

    # Filas entre estágios
    download_queue: queue.Queue = queue.Queue(maxsize=500)
    organize_queue: queue.Queue = queue.Queue()

    # Estado compartilhado
    stats        = Stats()
    seen_files   = set[str]()
    seen_lock    = threading.Lock()
    planned      = set[Path]()
    planned_lock = threading.Lock()

    # Writer do assuntos.csv (compartilhado pelos organize workers)
    csv_writer: CsvWriter | None = None
    if HAS_PDF_DEPS and not args.so_download and not args.no_csv:
        csv_writer = CsvWriter(R(args.csv_output), append=not args.fresh_csv)

    def enqueue_cached_items(cached_items: list[DownloadItem]) -> None:
        """Reinsere arquivos já descobertos em runs anteriores e evita duplicar no índice."""
        for item in cached_items:
            with seen_lock:
                if item.file_id in seen_files:
                    continue
                seen_files.add(item.file_id)
            with planned_lock:
                planned.add(item.destination)
            stats.inc("total")
            if _skip(item.destination, item.file_size):
                stats.inc("skipped")
            download_queue.put(item)

    cached_items = db.load_arquivos() if db else []
    complete_index = bool(db and db.is_index_complete() and cached_items and not args.refresh_index)

    # --continuar: retoma sem re-baixar o que já foi feito. Vale tanto p/ índice
    # em cache quanto p/ reindexação (o teste roda no download worker). Um voto
    # está "feito" se o arquivo já está na saída, ou se o cache diz que ele não
    # tem par (seria descartado de novo).
    skip_done = None
    if args.continuar:
        organizados = {p.name for p in ato_dir.rglob("*") if p.is_file()} if ato_dir.exists() else set()
        def skip_done(it: DownloadItem) -> bool:
            if it.destination.name in organizados:
                return True
            if db and db.get_pares(it.file_id) == []:   # processado e sem par
                return True
            return False
        print(f"--continuar: {len(organizados)} arquivos já em {ato_dir.name}/ serão pulados.", flush=True)

    if complete_index:
        total_folders = db.count_indexed_folders() if db else 0
        print(f"Cache de índice completo encontrado: {len(cached_items)} arquivos. Pulando indexação.", flush=True)
        print(f"Iniciando pipeline: {args.download_workers} download / {args.organize_workers} organize workers\n", flush=True)

    else:
        # Indexação normal via API do Drive
        service = _new_service(token_path)
        configs = []
        if "1" in instancias: configs.append(("1a_instancia","sess",is_1a))
        if "2" in instancias: configs.append(("2a_instancia","voto",is_2a))

        all_folders: list[tuple[dict, Path, str]] = []
        for subpasta, hint, match_fn in configs:
            print(f"Buscando pastas {subpasta}...", flush=True)
            folders = [f for f in _iter(
                service,
                f"(trashed=false) and (mimeType='{FOLDER_MIME}') and (name contains '{hint}')",
                "id,name,mimeType,shortcutDetails",
            ) if match_fn(f.get("name",""))]
            print(f"  {len(folders)} pastas encontradas", flush=True)
            inst_dir = output_dir / subpasta
            for f in folders:
                fname = _san(f.get("name", f["id"]))
                all_folders.append((f, inst_dir / f"{fname} - {f['id'][:8]}", subpasta))

        total_folders = len(all_folders)
        indexed_folder_ids = db.indexed_folder_ids() if db else set()
        if indexed_folder_ids:
            indexed_roots = sum(1 for folder, _, _ in all_folders if folder["id"] in indexed_folder_ids)
            if indexed_roots:
                stats.add("indexed", indexed_roots)
                print(f"Retomada: {indexed_roots}/{total_folders} pastas-raiz já indexadas.", flush=True)
        folders_to_index = [
            (folder, dest, subpasta)
            for folder, dest, subpasta in all_folders
            if folder["id"] not in indexed_folder_ids
        ]
        if cached_items:
            print(f"Retomada: {len(cached_items)} arquivos já descobertos serão reaproveitados.", flush=True)
        print(f"\nTotal de pastas-raiz: {total_folders}", flush=True)
        print(f"Iniciando pipeline: {args.index_workers} index / {args.download_workers} download / {args.organize_workers} organize workers\n", flush=True)

    # Display thread
    stop = threading.Event()
    disp = threading.Thread(
        target=_display,
        args=(stats, total_folders, download_queue, organize_queue, start_time, stop),
        daemon=True,
    )
    disp.start()

    sif = SifClient(rate=args.sif_rate) if HAS_PDF_DEPS and not args.so_download else None

    org_pool = ThreadPoolExecutor(max_workers=args.organize_workers, thread_name_prefix="org")
    org_futs = []
    if sif:
        org_futs = [org_pool.submit(organize_worker, organize_queue, sif, ato_dir, stats, csv_writer, db)
                    for _ in range(args.organize_workers)]

    dl_pool = ThreadPoolExecutor(max_workers=args.download_workers, thread_name_prefix="dl")
    dl_futs = [dl_pool.submit(download_worker, token_path, download_queue, organize_queue, stats, skip_done)
               for _ in range(args.download_workers)]

    enqueue_cached_items(cached_items)

    if not complete_index:
        idx_pool = ThreadPoolExecutor(max_workers=args.index_workers, thread_name_prefix="idx")
        idx_futs = {
            idx_pool.submit(
                index_worker, token_path, folder, dest, subpasta, allowed_mimes,
                download_queue, seen_files, seen_lock, planned, planned_lock, stats, db,
            ): folder
            for folder, dest, subpasta in folders_to_index
        }

        for fut in as_completed(idx_futs):
            try:
                fut.result()
            except Exception as exc:
                stats.add_error(f"Index: {exc}")
        idx_pool.shutdown(wait=True)

        if db:
            root_ids = {folder["id"] for folder, _, _ in all_folders}
            indexed_ids = db.indexed_folder_ids()
            complete = root_ids.issubset(indexed_ids)
            db.mark_index_complete(complete)
            if complete:
                print("Índice do Drive completo salvo no cache.", flush=True)
            else:
                missing = len(root_ids - indexed_ids)
                print(f"Índice parcial salvo ({missing} pastas-raiz pendentes).", flush=True)

    # Sinaliza fim para download workers
    download_queue.put(_SENTINEL)
    for fut in as_completed(dl_futs):
        try: fut.result()
        except Exception as exc: stats.add_error(f"DL worker: {exc}")
    dl_pool.shutdown(wait=True)

    print("\n[Downloads concluídos — aguardando organização drenar...]\n", flush=True)

    # Sinaliza fim para organize workers
    if sif:
        organize_queue.put(_SENTINEL)
        for fut in as_completed(org_futs):
            try: fut.result()
            except Exception as exc: stats.add_error(f"Org worker: {exc}")
    org_pool.shutdown(wait=True)

    if csv_writer:
        csv_writer.close()

    stop.set()
    disp.join(timeout=2)

    s = stats.snapshot()
    end_time = time.time()
    print("\n" + "=" * 60)
    print(f"Início           : {_fmt_datetime(start_time)}")
    print(f"Fim              : {_fmt_datetime(end_time)}")
    print(f"Tempo total      : {_fmt_duration(end_time - start_time)}")
    print(f"Arquivos achados : {s['total']}")
    print(f"Baixados (novos) : {s['downloaded']}")
    print(f"Já estavam       : {s['skipped']}")
    if s['pulados']:
        print(f"Pulados (--cont.): {s['pulados']}")
    print(f"Organizados      : {s['organized']}")
    print(f"Sem ato          : {s['sem_ato']}")
    print(f"Erros            : {s['errors']}")
    if csv_writer:
        print(f"assuntos.csv     : {csv_writer.rows} linhas -> {R(args.csv_output)}")
    if stats.ato_counter:
        print("\nPor ato:")
        for key, cnt in sorted(stats.ato_counter.items(), key=lambda x: -x[1]):
            inst, ato = key.split("/", 1)
            print(f"  {cnt:>6}  {'1ª' if '1a' in inst else '2ª'}  {ato}")
    if stats.errors:
        print("\nErros:")
        for e in stats.errors[:20]: print(f"  - {e}")
        if len(stats.errors) > 20: print(f"  ... e mais {len(stats.errors)-20}")

    return 0 if not stats.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
