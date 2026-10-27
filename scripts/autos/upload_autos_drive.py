#!/usr/bin/env python3
"""
Sobe os PDFs dos autos para a pasta Autos do Drive.

Par do baixar_autos_sif.py. Nao faz parte do pipeline.

    python3 scripts/autos/upload_autos_drive.py --so-login   # so o consentimento OAuth
    python3 scripts/autos/upload_autos_drive.py              # sobe tudo (paralelo)
    python3 scripts/autos/upload_autos_drive.py --so-manifesto

Escopo: precisa de 'drive' (leitura+escrita). O escopo 'drive.file' NAO serve —
com ele a aplicacao nao enxerga a pasta Autos criada pelo usuario e o upload
falha com "parent not found". O token vai para token_upload.json, separado do
token.json readonly que o pipeline usa.

E retomavel: lista o que ja esta na pasta e sobe so o que falta. No fim regrava
site_publico/Autos/manifest.json no formato drive ({auto: file_id}).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

RAIZ = Path(__file__).resolve().parents[2]
PASTA_LOCAL = RAIZ / "site_publico" / "Autos"
MANIFEST = PASTA_LOCAL / "manifest.json"
VOTOS = RAIZ / "site_data" / "votos.jsonl"
CREDS = RAIZ / "credentials.json"
TOKEN = RAIZ / "token_upload.json"

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_ID_PADRAO = "1QveZZHzY5Rj2UFZqYLe7DkNqTfhDFGiz"

TENTATIVAS = 5
ESPERA_BASE = 2.0


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def carregar_creds() -> Credentials:
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        print("\n>>> Abrindo o navegador para o consentimento do Google.")
        print(">>> Se nao abrir sozinho, use a URL impressa abaixo.\n", flush=True)
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES)
        creds = flow.run_local_server(port=0, open_browser=True, authorization_prompt_message="Abra esta URL: {url}")
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    TOKEN.chmod(0o600)
    return creds


class Servicos:
    """googleapiclient nao e thread-safe: um service por thread."""

    def __init__(self, creds: Credentials):
        self._creds = creds
        self._local = threading.local()

    def get(self):
        s = getattr(self._local, "service", None)
        if s is None:
            s = build("drive", "v3", credentials=self._creds, cache_discovery=False)
            self._local.service = s
        return s


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------


def listar_pasta(service, folder_id: str) -> dict[str, list[str]]:
    """{nome_sem_extensao_maiusculo: [file_id, ...]} — recursivo."""
    achados: dict[str, list[str]] = defaultdict(list)
    pilha = [folder_id]
    while pilha:
        atual, token = pilha.pop(), None
        while True:
            r = (
                service.files()
                .list(
                    q=f"'{atual}' in parents and trashed=false",
                    fields="nextPageToken,files(id,name,mimeType)",
                    pageSize=1000,
                    pageToken=token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            for it in r.get("files", []):
                if it.get("mimeType") == FOLDER_MIME:
                    pilha.append(it["id"])
                elif it["name"].lower().endswith(".pdf"):
                    achados[it["name"][:-4].upper()].append(it["id"])
            token = r.get("nextPageToken")
            if not token:
                break
    return achados


class Contagem:
    def __init__(self, total: int):
        self.lock = threading.Lock()
        self.total = total
        self.ok = 0
        self.erros = 0
        self.inicio = time.monotonic()

    def marcar(self, sucesso: bool) -> None:
        with self.lock:
            if sucesso:
                self.ok += 1
            else:
                self.erros += 1
            feitos = self.ok + self.erros
        if feitos % 100 == 0 or feitos == self.total:
            decorrido = time.monotonic() - self.inicio
            taxa = feitos / decorrido if decorrido else 0
            restam = (self.total - feitos) / taxa / 60 if taxa else 0
            print(
                f"  {feitos:>6}/{self.total} ({feitos / self.total * 100:5.1f}%)  "
                f"ok={self.ok} erros={self.erros}  {taxa:.1f}/s  "
                f"~{restam:.0f} min restantes",
                flush=True,
            )


def subir_um(
    caminho: Path, servicos: Servicos, folder_id: str, cont: Contagem, mapa: dict[str, str], erros: list[str]
) -> None:
    chave = caminho.stem.upper()
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            media = MediaFileUpload(str(caminho), mimetype="application/pdf", resumable=False)
            arquivo = (
                servicos.get()
                .files()
                .create(
                    body={"name": caminho.name, "parents": [folder_id]},
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
            with cont.lock:
                mapa[chave] = arquivo["id"]
            cont.marcar(True)
            return
        except HttpError as e:
            # 403 (rate limit) e 5xx sao transitorios; 404 no parent e fatal.
            status = getattr(e.resp, "status", 0)
            if status == 404:
                with cont.lock:
                    erros.append(f"{chave}\tpasta destino inacessivel (404) — escopo insuficiente?")
                cont.marcar(False)
                return
            if tentativa == TENTATIVAS:
                with cont.lock:
                    erros.append(f"{chave}\tHTTP {status}: {str(e)[:120]}")
                cont.marcar(False)
                return
            time.sleep(ESPERA_BASE * (2 ** (tentativa - 1)))
        except Exception as e:
            if tentativa == TENTATIVAS:
                with cont.lock:
                    erros.append(f"{chave}\t{type(e).__name__}: {str(e)[:120]}")
                cont.marcar(False)
                return
            time.sleep(ESPERA_BASE * (2 ** (tentativa - 1)))


# ---------------------------------------------------------------------------
# Manifesto
# ---------------------------------------------------------------------------


def autos_do_indice() -> set[str]:
    chaves: set[str] = set()
    with VOTOS.open(encoding="utf-8") as fh:
        for linha in fh:
            linha = linha.strip()
            if not linha:
                continue
            try:
                reg = json.loads(linha)
            except json.JSONDecodeError:
                continue
            for a in reg.get("autos") or []:
                idn = str(a.get("idn") or "").strip()
                if idn:
                    chaves.add(f"{idn}{str(a.get('tipo') or '').strip().upper()}")
    return chaves


def gravar_manifesto(mapa: dict[str, str], folder_id: str, forcar: bool) -> bool:
    esperados = autos_do_indice()
    cobertos = esperados & mapa.keys()
    faltando = esperados - mapa.keys()
    pct = len(cobertos) / len(esperados) * 100 if esperados else 0
    print(f"\nAutos no indice      : {len(esperados)}")
    print(f"No indice e no Drive : {len(cobertos)} ({pct:.1f}%)")
    print(f"No indice, sem Drive : {len(faltando)}")

    if pct < 90 and not forcar:
        print(f"\nCobertura {pct:.1f}% — manifesto NAO regravado (use --forcar).")
        return False

    if faltando:
        (PASTA_LOCAL / "_faltando_no_drive.txt").write_text("\n".join(sorted(faltando)), encoding="utf-8")

    MANIFEST.write_text(
        json.dumps(
            {
                "gerado_em": time.strftime("%Y-%m-%d %H:%M:%S"),
                "modo": "drive",
                "folder_id": folder_id,
                "total": len(mapa),
                "autos": mapa,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"{MANIFEST} regravado: {len(mapa)} autos, modo drive ({MANIFEST.stat().st_size / 1024:.0f} KB)")
    return True


# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(description="Sobe os autos para o Drive.")
    p.add_argument("--folder-id", default=FOLDER_ID_PADRAO)
    p.add_argument("--workers", type=int, default=24, help="Uploads em paralelo (padrao: 24)")
    p.add_argument("--limite", type=int, default=0, help="So os N primeiros (teste)")
    p.add_argument("--so-login", action="store_true", help="So faz o consentimento e sai")
    p.add_argument("--so-manifesto", action="store_true", help="Nao sobe nada; varre a pasta e regrava o manifesto")
    p.add_argument("--forcar", action="store_true", help="Regrava o manifesto com cobertura <90%%")
    args = p.parse_args()

    creds = carregar_creds()
    print(f"Autenticado. Escopos: {creds.scopes}", flush=True)
    if args.so_login:
        return 0

    servicos = Servicos(creds)
    print("Lendo o que ja existe na pasta do Drive...", flush=True)
    existentes = listar_pasta(servicos.get(), args.folder_id)
    mapa: dict[str, str] = {k: v[0] for k, v in existentes.items()}
    print(f"Ja no Drive: {len(mapa)} PDFs", flush=True)

    if args.so_manifesto:
        return 0 if gravar_manifesto(mapa, args.folder_id, args.forcar) else 1

    locais = sorted(PASTA_LOCAL.glob("*.pdf"))
    pendentes = [f for f in locais if f.stem.upper() not in mapa]
    if args.limite:
        pendentes = pendentes[: args.limite]
    print(
        f"Locais: {len(locais)} | a subir: {len(pendentes)} | {sum(f.stat().st_size for f in pendentes) / 1e6:.0f} MB",
        flush=True,
    )

    if not pendentes:
        print("Nada a subir.")
        return 0 if gravar_manifesto(mapa, args.folder_id, args.forcar) else 1

    cont = Contagem(len(pendentes))
    erros: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for caminho in pendentes:
            pool.submit(subir_um, caminho, servicos, args.folder_id, cont, mapa, erros)

    minutos = (time.monotonic() - cont.inicio) / 60
    print(f"\nUpload concluido em {minutos:.1f} min — ok={cont.ok} erros={cont.erros}")
    if erros:
        (PASTA_LOCAL / "_erros_upload.txt").write_text("\n".join(sorted(erros)), encoding="utf-8")
        print(f"Erros em {PASTA_LOCAL / '_erros_upload.txt'} — rode de novo para tentar so eles")
        for linha in erros[:3]:
            print("  ", linha)

    return 0 if gravar_manifesto(mapa, args.folder_id, args.forcar) else 1


if __name__ == "__main__":
    raise SystemExit(main())
