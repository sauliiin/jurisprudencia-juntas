"""Gerador do índice público (api/indice.py), configuração e serviço de busca."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from api import indice
from api.config import Settings
from api.logs import FormatoJson
from api.services import busca


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("Belo Horizonte, 7 de março de 2025.", "2025-03-07"),
        ("1º de Janeiro de 2024 e depois 15 de marco de 2024", "2024-03-15"),
        ("sem data", None),
        ("40 de maio de 2025", None),
        ("", None),
    ],
)
def test_data_da_decisao(texto: str, esperado: str | None) -> None:
    assert indice.data_da_decisao(texto) == esperado


def test_resultados_da_ia(tmp_path: Path) -> None:
    (tmp_path / "modelo.json").write_text(
        json.dumps({"ids": ["a", "b", "c"], "resultados": ["", "indeferido", "deferido"]}), encoding="utf-8"
    )
    (tmp_path / "resultados.bin").write_bytes(bytes([1, 0, 2]))
    assert indice.resultados_da_ia(tmp_path) == {"a": "indeferido", "c": "deferido"}
    assert indice.resultados_da_ia(tmp_path / "nao_existe") == {}


def test_pulados_do_marco(tmp_path: Path) -> None:
    marco = tmp_path / "marco.json"
    marco.write_text(json.dumps({"counts": {"pulados": 42}}), encoding="utf-8")
    assert indice.pulados_do_marco(marco) == 42
    assert indice.pulados_do_marco(tmp_path / "nao_existe.json") == 0


def test_arquivos_do_indice_privado(tmp_path: Path) -> None:
    privado = tmp_path / "privado.db"
    con = sqlite3.connect(privado)
    con.execute("CREATE TABLE votos (file_id TEXT, caminho_bruto TEXT)")
    con.executemany(
        "INSERT INTO votos VALUES (?, ?)",
        [("a", "/home/x/votos_brutos/1a_instancia/a.pdf"), ("b", "/outro/lugar/b.pdf"), ("c", None)],
    )
    con.commit()
    con.close()
    assert indice.arquivos_do_indice_privado(privado) == {"a": "1a_instancia/a.pdf"}


def test_main_gera_o_indice(tmp_path: Path, capsys) -> None:
    jsonl = tmp_path / "votos.jsonl"
    registro = {
        "file_id": "x1",
        "nome_arquivo": "x1.docx",
        "instancia": "1a_instancia",
        "decisao_instancia": "Decisão de 1ª instância",
        "texto": "Obra sem alvará. 3 de junho de 2025.",
        "autos": [{"numero": "1AI", "autuado": "NÃO PODE ENTRAR", "infracao": "OBRA"}],
    }
    jsonl.write_text(json.dumps(registro, ensure_ascii=False) + "\n\n", encoding="utf-8")
    saida = tmp_path / "saida" / "indice.db"
    codigo = indice.main(
        ["--jsonl", str(jsonl), "--saida", str(saida), "--ia", str(tmp_path), "--marco", str(tmp_path / "m.json")]
    )
    assert codigo == 0
    assert "1 decisões" in capsys.readouterr().out
    con = sqlite3.connect(saida)
    assert con.execute("SELECT data FROM votos").fetchone() == ("2025-06-03",)
    colunas = [linha[1] for linha in con.execute("PRAGMA table_info(autos)")]
    assert "autuado" not in colunas
    texto_fts = con.execute("SELECT autos_texto FROM votos_fts").fetchone()[0]
    assert "NÃO PODE ENTRAR" not in texto_fts
    con.close()


def test_main_sem_jsonl(tmp_path: Path) -> None:
    assert indice.main(["--jsonl", str(tmp_path / "nada.jsonl")]) == 2


def test_fts_query() -> None:
    assert busca.fts_query("obra sem alvará") == '"obra sem alvará"'
    assert busca.fts_query('diz "olá"') == '"diz ""olá"""'
    assert busca.fts_query("obra alvará", "words") == '"obra"* AND "alvará"*'
    assert busca.date_fts_query("março", "2025") == '"março de 2025"'
    assert busca.date_fts_query("", "2025") == '"de 2025"'
    assert busca.date_fts_query("março", "") == ""


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("https://a.com, https://b.com", ["https://a.com", "https://b.com"]),
        ('["https://a.com"]', ["https://a.com"]),
        ("", []),
    ],
)
def test_cors_por_variavel_de_ambiente(monkeypatch, valor: str, esperado: list[str]) -> None:
    monkeypatch.setenv("JURIS_CORS_ORIGINS", valor)
    assert Settings().cors_origins == esperado


def test_formato_json_dos_logs() -> None:
    registro = logging.makeLogRecord({"name": "api", "levelname": "INFO", "msg": "oi", "rota": "/x"})
    evento = json.loads(FormatoJson().format(registro))
    assert evento["msg"] == "oi"
    assert evento["rota"] == "/x"
    assert evento["ts"].endswith("Z")
