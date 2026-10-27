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

Com --legislacao, o mesmo script indexa a pasta de legislação (modo Legislação
do site) em site_data/legislacao.jsonl: texto inteiro, categoria = norma tirada do
nome do arquivo, redação revogada (riscada no original) marcada em "riscado" e o
texto dividido em artigos/itens em "dispositivos" (ver legislacao.py).

  python3 -u preparar_pareceres.py --legislacao
"""

from __future__ import annotations

import argparse
import csv
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
from legislacao import dividir, html_para_texto, norma_do_nome, sem_fora_do_bmp

PARECERES_FOLDER_ID = "1wE88R1FIYVZxgWAeCgukJFaSC3Irr7T6"
LEGISLACAO_FOLDER_ID = "1WUOuLv7-ddLERjUpNc7klpH9KjxNnzlm"

XLS_MIME = "application/vnd.ms-excel"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# mimeType -> rótulo amigável de extensão usado no JSONL / frontend.
# Arquivos enviados pelo WPS Office chegam ao Drive com mimeType próprio.
MIME_LABEL = {
    PDF_MIME: "pdf",
    DOCX_MIME: "docx",
    DOC_MIME: "doc",
    GDOC_MIME: "gdoc",
    XLS_MIME: "xls",
    XLSX_MIME: "xlsx",
    "application/wps-office.doc": "doc",
    "application/wps-office.docx": "docx",
    "application/wps-office.xls": "xls",
    "application/wps-office.xlsx": "xlsx",
}
WANTED_MIMES = set(MIME_LABEL)
PLANILHAS = {"xls", "xlsx"}
# Na legislação estes formatos passam pelo HTML do LibreOffice, que guarda o riscado.
COM_RISCADO = {"doc", "docx", "xls", "xlsx"}


@dataclass
class ParecerItem:
    file_id: str
    nome_arquivo: str
    mime: str
    file_size: str | None
    categoria: str  # subpasta de 1º nível (ou "Raiz"), ou a norma
    pasta: str  # caminho relativo completo dentro do banco
    destination: Path
    modificado_em: str = ""


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
    modificado_em: str = ""

    def como_dict(self) -> dict:
        dados = asdict(self)
        # Só a legislação tem redação riscada; os pareceres não ganham o campo.
        if hasattr(self, "riscado"):
            dados["riscado"] = self.riscado
        return dados


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
    legislacao: bool = False


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
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600, check=False)
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


def _texto_planilha(path: Path) -> str | None:
    """Converte .xls/.xlsx -> CSV (todas as abas) via LibreOffice e junta as linhas."""
    if not _pipeline._SOFFICE:
        return None
    with _pipeline._DOC_SEM:
        tmp = Path(tempfile.mkdtemp(prefix=".lo_", dir=Path.cwd()))
        try:
            prof, out = tmp / "profile", tmp / "out"
            out.mkdir()
            # 44=vírgula, 34=aspas, 76=UTF-8; o -1 final exporta cada aba num CSV.
            filtro = "csv:Text - txt - csv (StarCalc):44,34,76,1,,0,false,true,false,false,false,-1"
            subprocess.run(
                [
                    *_pipeline._SOFFICE,
                    "--headless",
                    f"-env:UserInstallation=file://{prof}",
                    "--convert-to",
                    filtro,
                    "--outdir",
                    str(out),
                    str(path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=False,
            )
            partes = []
            for csv_path in sorted(out.glob("*.csv")):
                with csv_path.open(encoding="utf-8", errors="ignore", newline="") as fh:
                    linhas = [" | ".join(c.strip() for c in row if c.strip()) for row in csv.reader(fh)]
                partes.append("\n".join(linha for linha in linhas if linha))
            texto = "\n\n".join(p for p in partes if p)
            return texto or None
        except Exception:
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _texto_html(path: Path) -> tuple[str, list[list[int]]] | None:
    """Documento -> HTML (LibreOffice) -> texto + trechos riscados. Falha => None."""
    if not _pipeline._SOFFICE:
        return None
    with _pipeline._DOC_SEM:
        tmp = Path(tempfile.mkdtemp(prefix=".lo_", dir=Path.cwd()))
        try:
            prof, out = tmp / "profile", tmp / "out"
            out.mkdir()
            subprocess.run(
                [
                    *_pipeline._SOFFICE,
                    "--headless",
                    f"-env:UserInstallation=file://{prof}",
                    "--convert-to",
                    "html",
                    "--outdir",
                    str(out),
                    str(path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=False,
            )
            htmls = list(out.glob("*.html"))
            if not htmls:
                return None
            return html_para_texto(htmls[0].read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def _ocr_documento(path: Path, ext: str, lang: str, dpi: int, max_pages: int) -> str:
    if ext == "pdf":
        return _ocr_pdf(path, lang, dpi, max_pages).strip()
    if ext == "docx":
        return _ocr_docx_imagens(path, lang).strip()
    return ""


def _walk(
    service,
    folder_id: str,
    rel_path: list[str],
    raw_dir: Path,
    out: list[ParecerItem],
    stats: WalkStats,
    por_norma: bool = False,
) -> None:
    """Percorre recursivamente o Drive coletando arquivos suportados."""
    children = list(
        _iter(
            service,
            f"('{_eq(folder_id)}' in parents) and (trashed=false)",
            "id,name,mimeType,size,modifiedTime,shortcutDetails",
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
            _walk(service, cid, [*rel_path, name], raw_dir, out, stats, por_norma)
        elif mime in WANTED_MIMES:
            categoria = rel_path[0] if rel_path else "Raiz"
            if por_norma:
                categoria = norma_do_nome(name) or categoria
            pasta = " / ".join(rel_path) if rel_path else "Raiz"
            ext = MIME_EXT.get(mime) or f".{MIME_LABEL[mime]}"
            stem = _san(Path(name).stem)[:140]
            destination = raw_dir / _san(categoria) / f"{stem} - {cid[:8]}{ext}"
            out.append(
                ParecerItem(
                    cid, name, mime, child.get("size"), categoria, pasta, destination, child.get("modifiedTime") or ""
                )
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
        dl_item = DownloadItem(item.file_id, item.nome_arquivo, item.file_size, item.destination, item.categoria)
        _do_download(self.service(), dl_item)


def process_item(item: ParecerItem, downloader: Downloader, opts: OcrOpts) -> ParecerRecord | None:
    downloader.ensure_file(item)
    dl_item = DownloadItem(item.file_id, item.nome_arquivo, item.file_size, item.destination, item.categoria)
    ext = MIME_LABEL.get(item.mime, "")
    riscado: list[list[int]] = []
    lido = _texto_html(item.destination) if opts.legislacao and ext in COM_RISCADO else None
    if lido and lido[0].strip():
        texto, riscado = lido
    elif ext in PLANILHAS:
        texto = (_texto_planilha(item.destination) or "").strip()
    else:
        texto = (_extrair_texto(dl_item) or "").strip()
    usou_ocr = False
    if not texto and opts.enabled:
        ocr_texto = _ocr_documento(item.destination, ext, opts.lang, opts.dpi, opts.max_pages)
        if ocr_texto:
            texto = ocr_texto
            usou_ocr = True
    # BOM que o LibreOffice deixa no .txt. Na legislação, as posições do riscado e dos
    # artigos precisam valer igual no navegador (ver legislacao.py).
    texto = sem_fora_do_bmp(texto) if opts.legislacao else texto.replace("\ufeff", "")
    if opts.max_chars and len(texto) > opts.max_chars:
        texto = texto[: opts.max_chars]
        riscado = [[a, min(b, len(texto))] for a, b in riscado if a < len(texto)]
    record = ParecerRecord(
        file_id=item.file_id,
        nome_arquivo=item.nome_arquivo,
        categoria=item.categoria,
        pasta=item.pasta,
        ext=ext,
        texto=texto,
        drive_view_url=_drive_view_url(item.file_id),
        drive_preview_url=_drive_preview_url(item.file_id),
        ocr=usou_ocr,
        modificado_em=item.modificado_em,
    )
    if opts.legislacao:
        record.riscado = riscado
    return record


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


def _export_jsonl(path: Path, records: dict[str, dict], legislacao: bool = False) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if legislacao:
        # Refeito a cada exportação: mudanças na divisão valem sem baixar nada de novo.
        for rec in records.values():
            rec["dispositivos"] = dividir(rec.get("texto") or "", rec.get("nome_arquivo") or "", rec.get("riscado"))
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
    parser.add_argument(
        "--legislacao",
        action="store_true",
        help="Indexa a pasta de legislação: texto inteiro, categoria = norma, "
        "redação revogada marcada e divisão em artigos.",
    )
    parser.add_argument("--folder", default=None)
    parser.add_argument("--brutos", default=None)
    parser.add_argument("--jsonl", default=None)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--doc-concurrency", type=int, default=1, help="Conversões .doc simultâneas via LibreOffice. 1 evita colisões."
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help="Limite de caracteres do texto por documento. 0 = sem limite (padrão: 120000; na legislação, sem limite).",
    )
    parser.add_argument(
        "--ocr",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="OCR (tesseract) como fallback para documentos escaneados.",
    )
    parser.add_argument("--ocr-lang", default="por+eng", help="Idiomas do tesseract.")
    parser.add_argument("--ocr-dpi", type=int, default=220, help="DPI ao rasterizar PDFs.")
    parser.add_argument(
        "--ocr-max-pages", type=int, default=15, help="Máximo de páginas a passar por OCR por PDF. 0 = todas."
    )
    parser.add_argument("--limit", type=int, default=0, help="Processa só N arquivos (teste).")
    parser.add_argument("--rebuild", action="store_true", help="Ignora o JSONL existente e reprocessa tudo.")
    args = parser.parse_args()
    leg = args.legislacao
    args.folder = args.folder or (LEGISLACAO_FOLDER_ID if leg else PARECERES_FOLDER_ID)
    args.brutos = args.brutos or ("legislacao_brutos" if leg else "pareceres_brutos")
    args.jsonl = args.jsonl or ("site_data/legislacao.jsonl" if leg else "site_data/pareceres.jsonl")
    if args.max_chars is None:
        args.max_chars = 0 if leg else 120000
    return args


def main() -> int:
    args = parse_args()
    base = Path.cwd()

    def resolve(v):
        return Path(v) if Path(v).is_absolute() else base / v

    # Serializa as conversões .doc do LibreOffice (flatpak colide em paralelo).
    _pipeline._DOC_SEM = threading.Semaphore(max(1, args.doc_concurrency))

    _load_creds(resolve(args.credentials), resolve(args.token))
    service = _new_service(resolve(args.token))

    raw_dir = resolve(args.brutos)
    jsonl_path = resolve(args.jsonl)

    print("Indexando estrutura do Drive...", flush=True)
    items: list[ParecerItem] = []
    stats = WalkStats()
    _walk(service, args.folder, [], raw_dir, items, stats, args.legislacao)
    print(f"Descobertos: {stats.files} arquivos em {stats.folders} subpastas.", flush=True)

    existing = {} if args.rebuild else _load_existing(jsonl_path)
    # Arquivos que saíram do Drive saem do índice (o link do site quebraria).
    no_drive = {it.file_id: it for it in items}
    removidos = [fid for fid in existing if fid not in no_drive]
    for fid in removidos:
        del existing[fid]
    if removidos:
        print(f"Removidos do índice (não estão mais no Drive): {len(removidos)}", flush=True)
    if existing:
        before = len(items)
        pendentes = []
        for it in items:
            rec = existing.get(it.file_id)
            alterado = bool(rec and rec.get("modificado_em") and rec["modificado_em"] != it.modificado_em)
            # Registro da legislação sem "riscado" veio de antes da leitura via HTML.
            sem_riscado = args.legislacao and rec is not None and "riscado" not in rec
            if not rec or not rec.get("texto") or alterado or sem_riscado:
                if alterado:
                    it.destination.unlink(missing_ok=True)
                pendentes.append(it)
            else:
                # Registros antigos ganham a data agora; a partir daí, edição no Drive reindexa.
                rec["modificado_em"] = it.modificado_em
                rec["categoria"] = it.categoria
        items = pendentes
        print(f"Retomada: {before - len(items)} já indexados serão reaproveitados.", flush=True)

    if args.limit > 0:
        items = items[: args.limit]

    if not items:
        exported = _export_jsonl(jsonl_path, existing, args.legislacao)
        print(f"Nada novo. JSONL com {exported} registros: {jsonl_path}", flush=True)
        return 0

    downloader = Downloader(resolve(args.token))
    ocr_opts = OcrOpts(
        enabled=args.ocr,
        lang=args.ocr_lang,
        dpi=args.ocr_dpi,
        max_pages=args.ocr_max_pages,
        max_chars=args.max_chars,
        legislacao=args.legislacao,
    )
    if args.ocr and not (_HAS_PDFTOPPM and _HAS_TESSERACT):
        print("[aviso] OCR pedido mas pdftoppm/tesseract ausentes; seguindo sem OCR.", flush=True)
    records: dict[str, dict] = dict(existing)
    ok = errors = ocr_count = 0
    started = time.time()
    total = len(items)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(process_item, it, downloader, ocr_opts): it for it in items}
        for idx, fut in enumerate(as_completed(futures), start=1):
            it = futures[fut]
            try:
                record = fut.result()
            except Exception as exc:
                errors += 1
                print(f"[erro] {it.nome_arquivo}: {exc}", flush=True)
                continue
            records[record.file_id] = record.como_dict()
            ok += 1
            if record.ocr:
                ocr_count += 1
            chars = len(record.texto)
            tag = "OCR " if record.ocr else ""
            if idx % 10 == 0 or idx == total:
                elapsed = max(time.time() - started, 0.001)
                rate = idx / elapsed * 60
                eta = (total - idx) / rate if rate else 0
                print(
                    f"[{idx}/{total}] ok={ok} ocr={ocr_count} erros={errors} | {rate:.1f} arq/min | ETA {eta:.1f} min",
                    flush=True,
                )
            else:
                print(f"[{idx}/{total}] {tag}{record.ext:4} {chars:>7} chars | {record.nome_arquivo}", flush=True)

    exported = _export_jsonl(jsonl_path, records, args.legislacao)
    print()
    print("Concluído.")
    print(f"Indexados (rodada) : {ok}")
    print(f"Via OCR            : {ocr_count}")
    print(f"Erros              : {errors}")
    print(f"Registros no JSONL : {exported}")
    print(f"JSONL              : {jsonl_path}")
    print(f"Tempo              : {(time.time() - started) / 60:.1f} min")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
