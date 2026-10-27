"""Conexão somente leitura com o índice público, uma por requisição."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import Depends

from api.config import Settings, get_settings


class IndiceIndisponivel(RuntimeError):
    """O arquivo do índice não existe ou não abre."""


def conectar(indice: Path) -> sqlite3.Connection:
    if not indice.is_file():
        raise IndiceIndisponivel(f"Índice não encontrado: {indice.name}")
    con = sqlite3.connect(f"file:{indice}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def get_db(settings: Annotated[Settings, Depends(get_settings)]) -> Iterator[sqlite3.Connection]:
    con = conectar(settings.indice)
    try:
        yield con
    finally:
        con.close()


Db = Annotated[sqlite3.Connection, Depends(get_db)]
