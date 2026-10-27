"""Configuração por variáveis de ambiente (prefixo JURIS_), com padrões para rodar local."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

RAIZ = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JURIS_", env_file=".env.api", extra="ignore")

    indice: Path = Field(
        default=RAIZ / "indice_publico.db",
        description="Índice SQLite público, gerado por `python -m api.indice` a partir do JSONL mascarado.",
    )
    site_dir: Path = Field(default=RAIZ / "site_publico", description="Arquivos estáticos do site.")
    dados_dir: Path = Field(default=RAIZ / "site_data", description="Acervos JSONL e modelo da IA.")
    arquivos_dir: Path | None = Field(
        default=None,
        description=(
            "Pasta com os arquivos originais das decisões. Sem ela, /api/file responde 404: "
            "os originais trazem nomes que o acervo público mascara."
        ),
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list, description="Origens liberadas no CORS (JSON ou separadas por vírgula)."
    )
    host: str = "127.0.0.1"
    port: int = 8000
    max_limit: int = Field(default=100, ge=1, description="Máximo de resultados por página.")
    log_level: str = "INFO"
    servir_site: bool = Field(default=True, description="Serve site_publico/ e site_data/ junto com a API.")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _separar_origens(cls, valor: object) -> object:
        # Aceita '["https://a.com"]' ou "https://a.com,https://b.com".
        if isinstance(valor, str):
            if valor.strip().startswith("["):
                return json.loads(valor)
            return [origem.strip() for origem in valor.split(",") if origem.strip()]
        return valor


@lru_cache
def get_settings() -> Settings:
    return Settings()
