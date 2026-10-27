"""Modelos das respostas. Os campos são os que o site (site_publico/app.js) já usa."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Instancia = Literal["todas", "1a_instancia", "2a_instancia"]
ModoBusca = Literal["phrase", "words", "any"]
Mes = Literal[
    "",
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
]


class Stats(BaseModel):
    votos: int = Field(description="Decisões no índice.")
    autos: int = Field(description="Autos de infração citados nas decisões.")
    pulados: int = Field(description="Documentos descartados pelo pipeline (não são decisões).")


class Auto(BaseModel):
    numero: str
    infracao: str = ""
    dispositivo_legal_transgredido: str = ""
    local_constatacao: str = ""
    lei: str = ""
    pdf_encontrado: bool = False


class VotoBase(BaseModel):
    file_id: str
    nome_arquivo: str = Field(description="Nome do arquivo, com o autuado já mascarado.")
    instancia: str
    decisao_instancia: str
    protocolo: str = ""
    assunto: str = ""
    data: str | None = Field(default=None, description="Data da decisão (AAAA-MM-DD), lida do texto.")
    resultado: str | None = Field(default=None, description="Resultado lido do dispositivo pelo modelo do site.")
    ext: str = ""
    file_url: str
    drive_file_id_publico: str | None = None
    drive_view_url: str | None = None
    drive_preview_url: str | None = None
    autos: list[Auto] = Field(default_factory=list)


class ItemBusca(VotoBase):
    trecho: str = Field(description="Trecho com os termos entre <mark>.")
    rank: float = Field(description="BM25 do FTS5 (menor = mais relevante); 0 sem consulta.")


class ResultadoBusca(BaseModel):
    items: list[ItemBusca]
    total: int
    limit: int
    offset: int


class Voto(VotoBase):
    texto_preview: str = Field(description="Texto integral da decisão, mascarado.")


class Health(BaseModel):
    status: Literal["ok", "degradado"]
    indice: bool = Field(description="Se o índice SQLite está acessível.")
    votos: int | None = None
    versao: str


class DetalheErro(BaseModel):
    codigo: str
    mensagem: str
    detalhes: list[dict] | None = None
    request_id: str | None = None


class Erro(BaseModel):
    erro: DetalheErro
