#!/usr/bin/env python3
"""
Indexa o "BANCO DE PARECERES GEJUD" do Google Drive para o site público.

Diferente de preparar_acervo_publico.py (que trata votos com autos/SIF), aqui os
documentos são pareceres/notas/ofícios. Não há consulta ao SIF nem extração de
autos: cada arquivo é baixado uma vez, tem o texto extraído e vira um registro
JSON para a busca estática.

Saídas:
  - pareceres_brutos/<categoria>/<arquivo>  (área de trabalho, fora do Git)
  - site_data/pareceres.jsonl               (índice estático do site)

O preview/abertura usa o próprio arquivo de origem no Drive (drive_view_url /
drive_preview_url), já que esta pasta é a fonte pública dos pareceres.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path

import baixar_e_organizar_por_ato as _pipeline
from baixar_e_organizar_por_ato import (
    DOC_MIME,
    DOCX_MIME,
    FOLDER_MIME,
    GDOC_MIME,
    MIME_EXT,
    PDF_MIME,
    DownloadItem,
    _do_download,
    _eq,
    _extrair_texto,
    _iter,
    _load_creds,
    _new_service,
    _resolve,
    _san,
)

PARECERES_FOLDER_ID = "1wE88R1FIYVZxgWAeCgukJFaSC3Irr7T6"

# mimeType -> rótulo amigável de extensão usado no JSONL / frontend.
MIME_LABEL = {PDF_MIME: "pdf", DOCX_MIME: "docx", DOC_MIME: "doc", GDOC_MIME: "gdoc"}
WANTED_MIMES = set(MIME_LABEL)


@dataclass
class ParecerItem:
    file_id: str
    nome_arquivo: str
    mime: str
    file_size: str | None
    categoria: str          # subpasta de 1º nível (ou "Raiz")
    pasta: str              # caminho relativo completo dentro do banco
    destination: Path


@dataclass
class ParecerRecord:
    file_id: str
    nome_arquivo: str
    categoria: str
    pasta: str
    ext: str
    texto: str
    drive_view_url: str
    drive_preview_url: str
    ocr: bool = False
    tem_arquivo_publico: bool = True


@dataclass
class WalkStats:
    folders: int = 0
    files: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class OcrOpts:
    enabled: bool = True
    lang: str = "por+eng"
    dpi: int = 220
    max_pages: int = 15
    max_chars: int = 120000


def _drive_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def _drive_preview_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/preview"


# --- OCR (fallback para documentos escaneados / só imagem) -------------------
# Usa pdftoppm (poppler) para rasterizar e tesseract para reconhecer texto.
# Serializado para não saturar a CPU com vários tesseract simultâneos.
_OCR_SEM = threading.Semaphore(2)
_HAS_PDFTOPPM = shutil.which("pdftoppm") is not None
_HAS_TESSERACT = shutil.which("tesseract") is not None


def _tesseract_image(img: Path, lang: str) -> str:
    try:
        proc = subprocess.run(
            ["tesseract", str(img), "stdout", "-l", lang, "--psm", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=120,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
        return proc.stdout.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _ocr_pdf(path: Path, lang: str, dpi: int, max_pages: int) -> str:
    if not (_HAS_PDFTOPPM and _HAS_TESSERACT):
        return ""
    with _OCR_SEM:
        tmp = Path(tempfile.mkdtemp(prefix=".ocr_", dir=Path.cwd()))
        try:
            prefix = tmp / "page"
            cmd = ["pdftoppm", "-png", "-r", str(dpi), "-f", "1"]
            if max_pages:
                cmd += ["-l", str(max_pages)]
            cmd += [str(path), str(prefix)]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=600, check=False)
            partes = [_tesseract_image(png, lang) for png in sorted(tmp.glob("page*.png"))]
            return "\n".join(p for p in partes if p.strip())
        except Exception:
            return ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _ocr_docx_imagens(path: Path, lang: str) -> str:
    if not _HAS_TESSERACT:
        return ""
    with _OCR_SEM:
        tmp = Path(tempfile.mkdtemp(prefix=".ocr_", dir=Path.cwd()))
        try:
            with zipfile.ZipFile(path) as zf:
                medias = [n for n in zf.namelist() if n.startswith("word/media/")]
                partes = []
                for name in medias:
                    suffix = Path(name).suffix.lower()
                    if suffix not in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}:
                        continue
                    dest = tmp / Path(name).name
                    dest.write_bytes(zf.read(name))
                    partes.append(_tesseract_image(dest, lang))
            return "\n".join(p for p in partes if p.strip())
        except Exception:
            return ""
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _ocr_documento(path: Path, ext: str, lang: str, dpi: int, max_pages: int) -> str:
    if ext == "pdf":
        return _ocr_pdf(path, lang, dpi, max_pages).strip()
    if ext == "docx":
        return _ocr_docx_imagens(path, lang).strip()
    return ""


def _walk(service, folder_id: str, rel_path: list[str], raw_dir: Path,
          out: list[ParecerItem], stats: WalkStats) -> None:
    """Percorre recursivamente o Drive coletando arquivos suportados."""
    children = list(
        _iter(
            service,
            f"('{_eq(folder_id)}' in parents) and (trashed=false)",
            "id,name,mimeType,size,shortcutDetails",
        )
    )
    for raw in children:
        child = _resolve(raw)
        cid = child.get("id")
        mime = child.get("mimeType")
        name = child.get("name") or cid
        if not cid:
            continue
        if mime == FOLDER_MIME:
            stats.folders += 1
            _walk(service, cid, rel_path + [name], raw_dir, out, stats)
        elif mime in WANTED_MIMES:
            categoria = rel_path[0] if rel_path else "Raiz"
            pasta = " / ".join(rel_path) if rel_path else "Raiz"
            ext = MIME_EXT.get(mime, "")
            stem = _san(Path(name).stem)[:140]
            destination = raw_dir / _san(categoria) / f"{stem} - {cid[:8]}{ext}"
            out.append(
                ParecerItem(cid, name, mime, child.get("size"), categoria, pasta, destination)
            )
            stats.files += 1


def _raw_exists(path: Path, size: str | None) -> bool:
    if not path.exists():
        return False
    if not size:
        return path.stat().st_size > 0
    try:
        return path.stat().st_size == int(size)
    except (OSError, ValueError):
        return path.stat().st_size > 0


class Downloader:
    def __init__(self, token_path: Path):
        self.token_path = token_path
        self._local = threading.local()

    def service(self):
        if not getattr(self._local, "service", None):
            self._local.service = _new_service(self.token_path)
        return self._local.service

    def ensure_file(self, item: ParecerItem) -> None:
        if _raw_exists(item.destination, item.file_size):
            return
        item.destination.parent.mkdir(parents=True, exist_ok=True)
        dl_item = DownloadItem(
            item.file_id, item.nome_arquivo, item.file_size, item.destination, item.categoria
        )
        _do_download(self.service(), dl_item)


def process_item(item: ParecerItem, downloader: Downloader, opts: "OcrOpts") -> ParecerRecord | None:
    downloader.ensure_file(item)
    dl_item = DownloadItem(
        item.file_id, item.nome_arquivo, item.file_size, item.destination, item.categoria
    )
    ext = MIME_LABEL.get(item.mime, "")
    texto = (_extrair_texto(dl_item) or "").strip()
    usou_ocr = False
    if not texto and opts.enabled:
        ocr_texto = _ocr_documento(item.destination, ext, opts.lang, opts.dpi, opts.max_pages)
        if ocr_texto:
            texto = ocr_texto
            usou_ocr = True
    if opts.max_chars and len(texto) > opts.max_chars:
        texto = texto[: opts.max_chars]
    return ParecerRecord(
        file_id=item.file_id,
        nome_arquivo=item.nome_arquivo,
        categoria=item.categoria,
        pasta=item.pasta,
        ext=ext,
        texto=texto,
        drive_view_url=_drive_view_url(item.file_id),
        drive_preview_url=_drive_preview_url(item.file_id),
        ocr=usou_ocr,
    )


def _load_existing(path: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    if not path.exists():
        return records
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            fid = rec.get("file_id")
            if fid:
                records[str(fid)] = rec
    return records


def _export_jsonl(path: Path, records: dict[str, dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    ordered = sorted(
        records.values(),
        key=lambda r: (r.get("categoria", ""), r.get("pasta", ""), r.get("nome_arquivo", "")),
    )
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in ordered:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return len(ordered)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Indexa o banco de pareceres do Drive.")
    parser.add_argument("--credentials", default="credentials.json")
    parser.add_argument("--token", default="token.json")
    parser.add_argument("--folder", default=PARECERES_FOLDER_ID)
    parser.add_argument("--brutos", default="pareceres_brutos")
    parser.add_argument("--jsonl", default="site_data/pareceres.jsonl")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--doc-concurrency", type=int, default=1,
                        help="Conversões .doc simultâneas via LibreOffice. 1 evita colisões.")
    parser.add_argument("--max-chars", type=int, default=120000,
                        help="Limite de caracteres do texto por documento. 0 = sem limite.")
    parser.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=True,
                        help="OCR (tesseract) como fallback para documentos escaneados.")
    parser.add_argument("--ocr-lang", default="por+eng", help="Idiomas do tesseract.")
    parser.add_argument("--ocr-dpi", type=int, default=220, help="DPI ao rasterizar PDFs.")
    parser.add_argument("--ocr-max-pages", type=int, default=15,
                        help="Máximo de páginas a passar por OCR por PDF. 0 = todas.")
    parser.add_argument("--limit", type=int, default=0, help="Processa só N arquivos (teste).")
    parser.add_argument("--rebuild", action="store_true",
                        help="Ignora o JSONL existente e reprocessa tudo.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base = Path.cwd()
    resolve = lambda v: Path(v) if Path(v).is_absolute() else base / v

    # Serializa as conversões .doc do LibreOffice (flatpak colide em paralelo).
    _pipeline._DOC_SEM = threading.Semaphore(max(1, args.doc_concurrency))

    _load_creds(resolve(args.credentials), resolve(args.token))
    service = _new_service(resolve(args.token))

    raw_dir = resolve(args.brutos)
    jsonl_path = resolve(args.jsonl)

    print("Indexando estrutura do Drive...", flush=True)
    items: list[ParecerItem] = []
    stats = WalkStats()
    _walk(service, args.folder, [], raw_dir, items, stats)
    print(f"Descobertos: {stats.files} arquivos em {stats.folders} subpastas.", flush=True)

    existing = {} if args.rebuild else _load_existing(jsonl_path)
    if existing:
        before = len(items)
        items = [it for it in items if it.file_id not in existing or not existing[it.file_id].get("texto")]
        print(f"Retomada: {before - len(items)} já indexados serão reaproveitados.", flush=True)

    if args.limit > 0:
        items = items[: args.limit]

    if not items:
        exported = _export_jsonl(jsonl_path, existing)
        print(f"Nada novo. JSONL com {exported} registros: {jsonl_path}", flush=True)
        return 0

    downloader = Downloader(resolve(args.token))
    ocr_opts = OcrOpts(
        enabled=args.ocr,
        lang=args.ocr_lang,
        dpi=args.ocr_dpi,
        max_pages=args.ocr_max_pages,
        max_chars=args.max_chars,
    )
    if args.ocr and not (_HAS_PDFTOPPM and _HAS_TESSERACT):
        print("[aviso] OCR pedido mas pdftoppm/tesseract ausentes; seguindo sem OCR.", flush=True)
    records: dict[str, dict] = dict(existing)
    ok = errors = ocr_count = 0
    started = time.time()
    total = len(items)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(process_item, it, downloader, ocr_opts): it for it in items
        }
        for idx, fut in enumerate(as_completed(futures), start=1):
            it = futures[fut]
            try:
                record = fut.result()
            except Exception as exc:
                errors += 1
                print(f"[erro] {it.nome_arquivo}: {exc}", flush=True)
                continue
            records[record.file_id] = asdict(record)
            ok += 1
            if record.ocr:
                ocr_count += 1
            chars = len(record.texto)
            tag = "OCR " if record.ocr else ""
            if idx % 10 == 0 or idx == total:
                elapsed = max(time.time() - started, 0.001)
                rate = idx / elapsed * 60
                eta = (total - idx) / rate if rate else 0
                print(f"[{idx}/{total}] ok={ok} ocr={ocr_count} erros={errors} | "
                      f"{rate:.1f} arq/min | ETA {eta:.1f} min", flush=True)
            else:
                print(f"[{idx}/{total}] {tag}{record.ext:4} {chars:>7} chars | {record.nome_arquivo}", flush=True)

    exported = _export_jsonl(jsonl_path, records)
    print()
    print("Concluído.")
    print(f"Indexados (rodada) : {ok}")
    print(f"Via OCR            : {ocr_count}")
    print(f"Erros              : {errors}")
    print(f"Registros no JSONL : {exported}")
    print(f"JSONL              : {jsonl_path}")
    print(f"Tempo              : {(time.time() - started)/60:.1f} min")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
