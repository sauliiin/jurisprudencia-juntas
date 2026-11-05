"""Configuração por variáveis de ambiente (prefixo JURIS_), com padrões para rodar local."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
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
    workers: int = Field(default=1, ge=1, description="Processos do uvicorn.")
    max_limit: int = Field(default=100, ge=1, description="Máximo de resultados por página.")
    log_level: str = "INFO"
    servir_site: bool = Field(default=True, description="Serve site_publico/ e site_data/ junto com a API.")

    # Perguntas sobre o Entendimento das Juntas (/api/perguntar).
    entendimento: Path | None = Field(
        default=None,
        description=(
            "Tópicos do Entendimento das Juntas, gerados por `python -m api.entendimento`. "
            "Vazio: dados_dir/entendimento.json."
        ),
    )
    llm_url: str = Field(
        default="https://api.groq.com/openai/v1/chat/completions",
        description="Endpoint de chat no formato da OpenAI (Groq, Gemini, OpenRouter, Cloudflare...).",
    )
    llm_modelo: str = Field(default="qwen/qwen3.8-27b", description="Modelo usado no endpoint.")
    llm_api_key: SecretStr | None = Field(default=None, description="Chave do serviço de IA (uso local).")
    llm_api_key_ssm: str | None = Field(
        default=None, description="Nome do parâmetro SecureString do SSM com a chave (uso no Lambda)."
    )
    llm_timeout: float = Field(default=12.0, gt=0, description="Segundos de espera pelo modelo.")
    llm_max_chars: int = Field(
        default=6000, ge=500, description="Máximo de caracteres do Entendimento enviados por pergunta."
    )

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
