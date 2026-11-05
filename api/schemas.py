"""Modelos das respostas. Os campos são os que o site (site_publico/app.js) já usa."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Instancia = Literal["todas", "1a_instancia", "2a_instancia"]
ModoBusca = Literal["phrase", "words", "any"]
Ordem = Literal["relevancia", "recentes", "antigas"]
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


class FonteEntendimento(BaseModel):
    n: int = Field(description="Número usado nas citações da resposta ([1], [2]...).")
    titulo: str = Field(description="Capítulo e tópicos do Entendimento, do mais geral ao mais específico.")
    texto: str = Field(description="Texto integral do tópico.")


class DocumentoEntendimento(BaseModel):
    arquivo: str
    link: str | None = Field(default=None, description="Documento original no Google Drive.")
    modificado_em: str | None = None


class RespostaPergunta(BaseModel):
    pergunta: str
    resposta: str | None = Field(
        description="Texto do modelo, com as citações [n] já conferidas; null se o modelo não respondeu."
    )
    aviso: str | None = Field(default=None, description="Observação para quem perguntou (sem tópico, sem IA...).")
    modelo: str | None = Field(default=None, description="Modelo que escreveu a resposta.")
    documento: DocumentoEntendimento
    fontes: list[FonteEntendimento] = Field(description="Tópicos consultados, na ordem das citações.")


class DetalheErro(BaseModel):
    codigo: str
    mensagem: str
    detalhes: list[dict] | None = None
    request_id: str | None = None


class Erro(BaseModel):
    erro: DetalheErro
