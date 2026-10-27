"""Fixtures: índice SQLite pequeno com decisões fictícias e a API apontando para ele."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.indice import gerar_indice
from api.main import create_app


def _voto(file_id: str, instancia: str, texto: str, autos: list[dict], **extra) -> dict:
    numero = "1ª" if instancia == "1a_instancia" else "2ª"
    return {
        "file_id": file_id,
        "nome_arquivo": f"{file_id} - [AUTUADO].pdf",
        "instancia": instancia,
        "decisao_instancia": f"Decisão de {numero} instância",
        "protocolo": f"31.0000{file_id[-1]}/2025-00",
        "assunto": f"Cancelamento do auto {autos[0]['numero']}" if autos else "",
        "texto": texto,
        "autos": autos,
        **extra,
    }


def _auto(numero: str, infracao: str, lei: str = "LEI 9725/09") -> dict:
    return {
        "numero": numero,
        "infracao": infracao,
        "dispositivo_legal_transgredido": f"{lei} - ART. 12",
        "local_constatacao": "RUA FICTÍCIA, Nº 1 - CENTRO",
        "lei": lei,
        "pdf_encontrado": True,
    }


VOTOS = [
    _voto(
        "voto1",
        "1a_instancia",
        "O recorrente executou obra sem alvará de construção. DISPOSITIVO DA DECISÃO: indefiro. "
        "Belo Horizonte, 10 de março de 2025.",
        [_auto("20250000001AI", "EXECUTAR OBRA SEM ALVARÁ")],
        drive_file_id_publico="drivePublico1",
    ),
    _voto(
        "voto2",
        "1a_instancia",
        "Obra sem alvará regularizada no prazo. Defiro o pedido. Belo Horizonte, 5 de abril de 2025.",
        [_auto("20250000002AI", "EXECUTAR OBRA SEM ALVARÁ")],
    ),
    _voto(
        "voto3",
        "2a_instancia",
        "Passeio irregular em frente ao imóvel; o alvará não se aplica. 20 de março de 2025.",
        [_auto("20250000003AN", "DEIXAR DE CONSTRUIR PASSEIO", "LEI 8616/03")],
    ),
    _voto(
        "voto4",
        "2a_instancia",
        "Pedido de prazo para regularização do passeio. 2 de fevereiro de 2024.",
        [_auto("20240000004AN", "DEIXAR DE CONSTRUIR PASSEIO", "LEI 8616/03")],
    ),
    _voto(
        "voto5",
        "1a_instancia",
        "Mesas e cadeiras no logradouro sem licença. 15 de janeiro de 2024.",
        [_auto("20240000005AI", "UTILIZAR MESA E CADEIRA SEM LICENÇA", "LEI 8616/03")],
    ),
    _voto(
        "voto6",
        "1a_instancia",
        "Engenho de publicidade sem licença. Sem data por extenso.",
        [],
    ),
    _voto(
        "voto7",
        "2a_instancia",
        "Recurso sobre obra sem alvará na segunda instância. 30 de março de 2025.",
        # Um JSONL antigo ainda com o autuado: o índice público tem de descartá-lo.
        [{**_auto("20250000007AI", "EXECUTAR OBRA SEM ALVARÁ"), "autuado": "FULANO DE TAL"}],
    ),
]


@pytest.fixture(scope="session")
def pasta_dados(tmp_path_factory: pytest.TempPathFactory) -> Path:
    raiz = tmp_path_factory.mktemp("acervo")
    resultados = {"voto1": "indeferido", "voto2": "deferido"}
    gerar_indice(
        [dict(v, autos=[dict(a) for a in v["autos"]]) for v in VOTOS],
        raiz / "indice_publico.db",
        resultados=resultados,
        arquivos={"voto1": "1a_instancia/voto1.pdf", "voto2": "../fora.pdf"},
        pulados=3,
        origem="teste",
    )
    arquivos = raiz / "arquivos" / "1a_instancia"
    arquivos.mkdir(parents=True)
    (arquivos / "voto1.pdf").write_bytes(b"%PDF-1.4 teste")
    (raiz / "fora.pdf").write_bytes(b"fora da pasta")
    site = raiz / "site"
    site.mkdir()
    (site / "index.html").write_text("<!doctype html><title>site</title>", encoding="utf-8")
    dados = raiz / "dados"
    dados.mkdir()
    (dados / "votos.jsonl").write_text("{}\n", encoding="utf-8")
    return raiz


@pytest.fixture
def settings(pasta_dados: Path) -> Settings:
    return Settings(
        indice=pasta_dados / "indice_publico.db",
        site_dir=pasta_dados / "site",
        dados_dir=pasta_dados / "dados",
        arquivos_dir=pasta_dados / "arquivos",
        max_limit=5,
        cors_origins=["https://juris.omniai.com.br"],
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as cliente:
        yield cliente
