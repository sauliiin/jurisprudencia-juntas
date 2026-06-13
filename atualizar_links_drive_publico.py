#!/usr/bin/env python3
"""
Atualiza site_data/votos.jsonl com links de preview/abertura da pasta pública
votos_brutos no Google Drive.

O índice original guarda o file_id do Drive de origem. Quando os arquivos são
copiados/enviados para uma nova pasta pública, eles ganham outros IDs. Este
script lista a pasta pública, casa os arquivos pelo nome gerado em
votos_brutos/<instancia>/ e grava drive_preview_url/drive_view_url no JSONL.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from baixar_e_organizar_por_ato import FOLDER_MIME, _load_creds, _new_service

MARCO_DEFAULT = "marco_atualizacao.json"


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


def _iter_children(service, folder_id: str):
    page_token = None
    while True:
        response = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken,files(id,name,mimeType,size)",
                pageSize=1000,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        yield from response.get("files", [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break


def listar_arquivos(service, root_folder_id: str) -> dict[str, list[dict]]:
    by_name: dict[str, list[dict]] = defaultdict(list)
    stack = [root_folder_id]
    folders = 0
    files = 0
    while stack:
        folder_id = stack.pop()
        folders += 1
        for item in _iter_children(service, folder_id):
            if item.get("mimeType") == FOLDER_MIME:
                stack.append(item["id"])
                continue
            by_name[item["name"]].append(item)
            files += 1
        if folders % 25 == 0:
            print(f"Pastas lidas: {folders} | arquivos: {files}", flush=True)
    print(f"Drive público: {folders} pastas | {files} arquivos", flush=True)
    return by_name


def _drive_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{quote(file_id, safe='')}/view"


def _drive_preview_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{quote(file_id, safe='')}/preview"


def atualizar_jsonl(jsonl_path: Path, by_name: dict[str, list[dict]]) -> tuple[int, int, int]:
    tmp = jsonl_path.with_suffix(jsonl_path.suffix + ".part")
    total = 0
    matched = 0
    duplicates = 0

    with jsonl_path.open(encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
        for line in src:
            if not line.strip():
                continue
            total += 1
            record = json.loads(line)
            local_name = Path(record.get("caminho_bruto", "")).name
            matches = by_name.get(local_name, [])
            if matches:
                if len(matches) > 1:
                    duplicates += 1
                drive_id = matches[0]["id"]
                record["drive_file_id_publico"] = drive_id
                record["drive_view_url"] = _drive_view_url(drive_id)
                record["drive_preview_url"] = _drive_preview_url(drive_id)
                matched += 1
            dst.write(json.dumps(record, ensure_ascii=False) + "\n")
            if total % 1000 == 0:
                print(f"JSONL: {total} registros | links públicos: {matched}", flush=True)

    tmp.replace(jsonl_path)
    return total, matched, duplicates


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Atualiza links públicos do Drive no JSONL do site.")
    parser.add_argument("folder_id", help="ID da pasta pública votos_brutos no Google Drive.")
    parser.add_argument("--jsonl", default="site_data/votos.jsonl")
    parser.add_argument("--marco", default=MARCO_DEFAULT)
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--token", default="token.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    jsonl_path = Path(args.jsonl)
    if not jsonl_path.exists():
        print(f"JSONL não encontrado: {jsonl_path}")
        return 2

    _load_creds(Path(args.credentials), Path(args.token))
    service = _new_service(Path(args.token))
    by_name = listar_arquivos(service, args.folder_id)
    total, matched, duplicates = atualizar_jsonl(jsonl_path, by_name)

    missing = total - matched
    marco_path = Path(args.marco)
    marco = _read_json(marco_path)
    counts = dict(marco.get("counts") or {})
    counts["jsonl_registros"] = total
    counts["jsonl_com_link_publico"] = matched
    marco.update(
        {
            "schema": marco.get("schema", 1),
            "updated_at": _now_iso(),
            "last_writer": "atualizar_links_drive_publico.py",
            "public_drive": {
                "folder_id": args.folder_id,
                "matched": matched,
                "missing": missing,
                "duplicate_names": duplicates,
            },
            "counts": counts,
        }
    )
    if "description" not in marco:
        marco["description"] = (
            "Marco versionado de retomada do acervo público. "
            "IDs em processed_file_ids são pulados nas próximas execuções, "
            "salvo com --rebuild ou --ignorar-marco."
        )
    _write_json_atomic(marco_path, marco)

    print()
    print("Concluído.")
    print(f"Registros JSONL       : {total}")
    print(f"Com link público      : {matched}")
    print(f"Sem correspondência   : {missing}")
    print(f"Nomes duplicados      : {duplicates}")
    print(f"Marco atualizado      : {marco_path}")
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
