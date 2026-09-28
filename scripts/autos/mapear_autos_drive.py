#!/usr/bin/env python3
"""
Mapeia os autos ja enviados ao Drive.

Par do baixar_autos_sif.py. Rode DEPOIS de subir a pasta Autos/ no Drive.
Nao faz upload e nao precisa de escopo de escrita: usa o token.json readonly
que o projeto ja tem.

    python3 scripts/autos/mapear_autos_drive.py                 # usa a pasta Autos padrao
    python3 scripts/autos/mapear_autos_drive.py --folder-id XXX # outra pasta

Varre a pasta (recursivamente, entao subpastas por tipo funcionam), casa o nome
do arquivo com a chave do auto — "20220057378AI.pdf" -> "20220057378AI" — e
regrava site_publico/Autos/manifest.json no formato do Drive:

    {"modo": "drive", "autos": {"20220057378AI": "<file_id>", ...}}

O app.js aceita os dois formatos: lista = PDFs locais, objeto = links do Drive.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from googleapiclient.discovery import build

from baixar_e_organizar_por_ato import _load_creds

RAIZ = Path(__file__).resolve().parents[2]
VOTOS = RAIZ / "site_data" / "votos.jsonl"
MANIFEST = RAIZ / "site_publico" / "Autos" / "manifest.json"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Pasta "Autos" dentro de "JURISPRUDÊNCIA - Todas decisões de 1ª e 2ª instâncias
# dividas por Lei". Localizada em 2026-07-29.
FOLDER_ID_PADRAO = "1QveZZHzY5Rj2UFZqYLe7DkNqTfhDFGiz"


def _servico():
    creds = _load_creds(RAIZ / "credentials.json", RAIZ / "token.json")
    return build("drive", "v3", credentials=creds)


def varrer(service, folder_id: str) -> dict[str, list[str]]:
    """{chave_do_auto: [file_id, ...]} — lista para detectar duplicatas."""
    achados: dict[str, list[str]] = defaultdict(list)
    pilha = [folder_id]
    pastas = arquivos = 0
    while pilha:
        atual = pilha.pop()
        pastas += 1
        token = None
        while True:
            resp = (
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
            for item in resp.get("files", []):
                if item.get("mimeType") == FOLDER_MIME:
                    pilha.append(item["id"])
                    continue
                nome = item["name"]
                if not nome.lower().endswith(".pdf"):
                    continue
                achados[nome[:-4].upper()].append(item["id"])
                arquivos += 1
            token = resp.get("nextPageToken")
            if not token:
                break
        if pastas % 10 == 0:
            print(f"  pastas: {pastas} | PDFs: {arquivos}", flush=True)
    print(f"Drive: {pastas} pasta(s), {arquivos} PDFs", flush=True)
    return achados


def autos_do_indice() -> set[str]:
    chaves: set[str] = set()
    with VOTOS.open(encoding="utf-8") as fh:
        for linha in fh:
            linha = linha.strip()
            if not linha:
                continue
            try:
                registro = json.loads(linha)
            except json.JSONDecodeError:
                continue
            for auto in registro.get("autos") or []:
                idn = str(auto.get("idn") or "").strip()
                if idn:
                    chaves.add(f"{idn}{str(auto.get('tipo') or '').strip().upper()}")
    return chaves


def main() -> int:
    p = argparse.ArgumentParser(description="Mapeia autos do Drive para o manifest.json.")
    p.add_argument("--folder-id", default=FOLDER_ID_PADRAO, help="Pasta Autos no Drive")
    p.add_argument("--forcar", action="store_true",
                   help="Regrava o manifesto mesmo com cobertura baixa (<90%%)")
    args = p.parse_args()

    esperados = autos_do_indice()
    print(f"Autos no indice: {len(esperados)}", flush=True)

    achados = varrer(_servico(), args.folder_id)

    duplicados = {k: v for k, v in achados.items() if len(v) > 1}
    mapa = {k: v[0] for k, v in achados.items()}
    cobertos = esperados & mapa.keys()
    faltando = esperados - mapa.keys()
    sobrando = mapa.keys() - esperados

    pct = len(cobertos) / len(esperados) * 100 if esperados else 0
    print(f"\nNo indice e no Drive : {len(cobertos)} ({pct:.1f}%)")
    print(f"No indice, sem Drive : {len(faltando)}")
    print(f"No Drive, fora do indice: {len(sobrando)}")
    if duplicados:
        print(f"[aviso] {len(duplicados)} auto(s) com mais de um arquivo no Drive; usando o primeiro")

    # Guarda-costas: nao troca um manifesto bom por um vazio se o upload ainda
    # estiver em andamento ou a pasta estiver errada.
    if pct < 90 and not args.forcar:
        print(f"\nCobertura de {pct:.1f}% — manifesto NAO regravado.")
        print("Se o upload ainda esta rodando, espere e rode de novo. Para forcar: --forcar")
        return 1

    if faltando:
        (MANIFEST.parent / "_faltando_no_drive.txt").write_text(
            "\n".join(sorted(faltando)), encoding="utf-8"
        )

    MANIFEST.write_text(
        json.dumps(
            {
                "gerado_em": time.strftime("%Y-%m-%d %H:%M:%S"),
                "modo": "drive",
                "folder_id": args.folder_id,
                "total": len(mapa),
                "autos": mapa,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\n{MANIFEST} regravado: {len(mapa)} autos, modo drive "
          f"({MANIFEST.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
