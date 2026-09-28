#!/usr/bin/env python3
"""
Baixa os PDFs dos autos do SIF para a pasta Autos/.

Nao faz parte do pipeline (atualizar_tudo.sh): rode sob demanda quando houver
autos novos. Precisa de SIF_USER e SIF_PASS no ambiente (ou no .env).

    python3 scripts/autos/baixar_autos_sif.py --limite 20     # teste rapido
    python3 scripts/autos/baixar_autos_sif.py                 # tudo (~13k autos)

Le os autos de site_data/votos.jsonl (campos idn/tipo/numero), consulta o SIF
uma vez por auto unico e grava site_publico/Autos/<IDN><TIPO>.pdf. E retomavel:
arquivo ja existente e nao-vazio nao e rebaixado. No fim escreve
site_publico/Autos/manifest.json com a lista dos autos disponiveis — e esse
manifesto que o site usa para decidir se aponta o link para o PDF local ou para
o SIF. A pasta fica dentro de site_publico/ para o link relativo "Autos/x.pdf"
valer tanto no GitHub Pages quanto no site_server.py local.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

RAIZ = Path(__file__).resolve().parents[2]
VOTOS = RAIZ / "site_data" / "votos.jsonl"

SIF_BASE = "https://sif-piloto.pbh.gov.br"
LOGIN_URL = f"{SIF_BASE}/Login.php?comp=1&ccsForm=Login"
AUTO_URL = f"{SIF_BASE}/MostraAuto.php"
LOGIN_USER = os.environ.get("SIF_USER", "")
LOGIN_PASS = os.environ.get("SIF_PASS", "")

TENTATIVAS = 4
ESPERA_BASE = 2.0  # segundos, dobra a cada tentativa


# ---------------------------------------------------------------------------
# Coleta dos autos unicos
# ---------------------------------------------------------------------------

def coletar_autos(caminho: Path) -> list[tuple[str, str, str]]:
    """Retorna [(chave, idn, tipo)] sem repeticao, na ordem em que aparecem."""
    vistos: dict[str, tuple[str, str, str]] = {}
    with caminho.open(encoding="utf-8") as fh:
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
                tipo = str(auto.get("tipo") or "").strip().upper()
                if not idn:
                    # fallback: deriva de numero (ex.: "20220057378AI")
                    numero = re.sub(r"[^A-Za-z0-9]", "", str(auto.get("numero") or "")).upper()
                    m = re.match(r"^(\d+)([A-Z]*)$", numero)
                    if not m:
                        continue
                    idn, tipo = m.group(1), m.group(2)
                chave = f"{idn}{tipo}"
                vistos.setdefault(chave, (chave, idn, tipo))
    return list(vistos.values())


# ---------------------------------------------------------------------------
# Cliente SIF
# ---------------------------------------------------------------------------

class Throttle:
    """Limite global de requisicoes por segundo, somando todos os workers."""

    def __init__(self, taxa: float):
        self._intervalo = 1.0 / max(taxa, 0.1)
        self._lock = threading.Lock()
        self._ultimo = 0.0

    def espera(self) -> None:
        with self._lock:
            falta = self._intervalo - (time.monotonic() - self._ultimo)
            if falta > 0:
                time.sleep(falta)
            self._ultimo = time.monotonic()


class SifClient:
    """Uma sessao autenticada por thread (o SIF usa cookie de sessao)."""

    def __init__(self, throttle: Throttle):
        self._throttle = throttle
        self._local = threading.local()

    def _sessao(self) -> requests.Session:
        sessao = getattr(self._local, "sessao", None)
        if sessao is None:
            sessao = requests.Session()
            sessao.headers["User-Agent"] = "Mozilla/5.0"
            sessao.post(
                LOGIN_URL,
                data={"login": LOGIN_USER, "password": LOGIN_PASS, "Button_DoLogin": "Login"},
                timeout=30,
            ).raise_for_status()
            self._local.sessao = sessao
        return sessao

    def _derruba_sessao(self) -> None:
        self._local.sessao = None

    def baixar(self, idn: str, tipo: str) -> bytes | None:
        """PDF do auto, ou None quando o SIF nao tem o auto (resposta sem %PDF)."""
        for tentativa in range(1, TENTATIVAS + 1):
            try:
                self._throttle.espera()
                resp = self._sessao().get(
                    AUTO_URL, params={"Idn_Doct_Lavr": idn, "Tip_Auto": tipo}, timeout=60
                )
                resp.raise_for_status()
                inicio = resp.content.find(b"%PDF")
                if inicio == -1:
                    return None  # auto inexistente: resposta legitima, nao e erro
                return resp.content[inicio:]
            except Exception as erro:  # rede/HTTP/sessao expirada
                self._derruba_sessao()
                if tentativa == TENTATIVAS:
                    raise
                time.sleep(ESPERA_BASE * (2 ** (tentativa - 1)))
                print(f"  [retry {tentativa}] {idn}{tipo}: {erro}", flush=True)
        return None


# ---------------------------------------------------------------------------
# Execucao
# ---------------------------------------------------------------------------

class Contagem:
    def __init__(self):
        self.lock = threading.Lock()
        self.ok = 0
        self.pulados = 0
        self.ausentes = 0
        self.erros = 0

    def total(self) -> int:
        return self.ok + self.pulados + self.ausentes + self.erros


def baixar_um(alvo: tuple[str, str, str], sif: SifClient, destino: Path,
              cont: Contagem, total: int, ausentes: list[str], erros: list[str]) -> None:
    chave, idn, tipo = alvo
    arquivo = destino / f"{chave}.pdf"
    if arquivo.exists() and arquivo.stat().st_size > 0:
        with cont.lock:
            cont.pulados += 1
        return
    try:
        conteudo = sif.baixar(idn, tipo)
    except Exception as erro:
        with cont.lock:
            cont.erros += 1
            erros.append(f"{chave}\t{type(erro).__name__}: {erro}")
        return

    if conteudo is None:
        with cont.lock:
            cont.ausentes += 1
            ausentes.append(chave)
        return

    parcial = arquivo.with_suffix(".pdf.part")
    parcial.write_bytes(conteudo)
    parcial.replace(arquivo)  # troca atomica: nunca sobra .pdf truncado
    with cont.lock:
        cont.ok += 1
        feitos = cont.total()
    if feitos % 50 == 0 or feitos == total:
        pct = feitos / total * 100
        print(
            f"  {feitos:>6}/{total} ({pct:5.1f}%)  "
            f"ok={cont.ok} pulados={cont.pulados} ausentes={cont.ausentes} erros={cont.erros}",
            flush=True,
        )


def escrever_manifesto(destino: Path) -> int:
    """Manifesto = o que realmente existe no disco (nao o que o script baixou)."""
    chaves = sorted(p.stem for p in destino.glob("*.pdf") if p.stat().st_size > 0)
    manifesto = destino / "manifest.json"

    # Os autos foram para o Drive em 2026-07-29 e os PDFs locais, apagados. Sem
    # esta trava, um --so-manifesto trocaria o mapa do Drive por uma lista vazia
    # e derrubaria todos os links do site.
    if not chaves and manifesto.exists():
        atual = json.loads(manifesto.read_text(encoding="utf-8"))
        if atual.get("modo") == "drive":
            raise SystemExit(
                f"{manifesto} esta em modo drive com {atual.get('total')} autos e nao ha "
                f"PDF local em {destino}.\nRegravar aqui apagaria os links do Drive. "
                f"Para voltar a servir local, baixe os PDFs antes."
            )

    manifesto.write_text(
        json.dumps(
            {"gerado_em": time.strftime("%Y-%m-%d %H:%M:%S"), "total": len(chaves), "autos": chaves},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return len(chaves)


def main() -> int:
    p = argparse.ArgumentParser(description="Baixa os PDFs dos autos do SIF para Autos/.")
    p.add_argument("--saida", default=str(RAIZ / "site_publico" / "Autos"),
                   help="Pasta de destino (padrao: site_publico/Autos/)")
    p.add_argument("--workers", type=int, default=8, help="Downloads em paralelo (padrao: 8)")
    p.add_argument("--taxa", type=float, default=8.0, help="Requisicoes/s no total (padrao: 8)")
    p.add_argument("--limite", type=int, default=0, help="So os N primeiros autos (teste)")
    p.add_argument("--so-manifesto", action="store_true",
                   help="Nao baixa nada; so regrava Autos/manifest.json")
    args = p.parse_args()

    destino = Path(args.saida)
    destino.mkdir(parents=True, exist_ok=True)

    if args.so_manifesto:
        print(f"manifest.json regravado com {escrever_manifesto(destino)} autos.")
        return 0

    if not VOTOS.exists():
        print(f"Nao encontrei {VOTOS}", file=sys.stderr)
        return 1

    alvos = coletar_autos(VOTOS)
    if args.limite:
        alvos = alvos[: args.limite]
    total = len(alvos)
    print(f"Autos unicos: {total}  ->  {destino}", flush=True)

    cont = Contagem()
    ausentes: list[str] = []
    erros: list[str] = []
    sif = SifClient(Throttle(args.taxa))
    inicio = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for alvo in alvos:
            pool.submit(baixar_um, alvo, sif, destino, cont, total, ausentes, erros)

    minutos = (time.monotonic() - inicio) / 60
    bytes_totais = sum(f.stat().st_size for f in destino.glob("*.pdf"))
    print(
        f"\nConcluido em {minutos:.1f} min — "
        f"baixados={cont.ok} pulados={cont.pulados} "
        f"sem PDF no SIF={cont.ausentes} erros={cont.erros}"
    )
    print(f"Pasta: {bytes_totais / 1e6:.0f} MB")

    if ausentes:
        (destino / "_sem_pdf.txt").write_text("\n".join(sorted(ausentes)), encoding="utf-8")
        print(f"Sem PDF no SIF listados em {destino / '_sem_pdf.txt'}")
    if erros:
        (destino / "_erros.txt").write_text("\n".join(sorted(erros)), encoding="utf-8")
        print(f"Erros listados em {destino / '_erros.txt'} — rode de novo para tentar so eles")

    print(f"manifest.json com {escrever_manifesto(destino)} autos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
