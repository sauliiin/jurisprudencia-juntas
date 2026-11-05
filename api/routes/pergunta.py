"""/api/perguntar: pergunta em linguagem natural respondida a partir do Entendimento das Juntas."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from api.config import Settings, get_settings
from api.schemas import Erro, RespostaPergunta
from api.services import pergunta

router = APIRouter(prefix="/api", tags=["perguntas"])


@router.get(
    "/perguntar",
    response_model=RespostaPergunta,
    summary="Pergunta sobre o Entendimento das Juntas",
    responses={422: {"model": Erro}, 503: {"model": Erro}},
)
def perguntar(
    settings: Annotated[Settings, Depends(get_settings)],
    q: Annotated[str, Query(min_length=3, max_length=500, description="A pergunta, em português.")],
) -> dict:
    """Acha os tópicos do Entendimento que tratam da pergunta e pede ao modelo uma resposta que só use
    esses tópicos, citando cada um. Sem o modelo, devolve só os tópicos (com `aviso`)."""
    return pergunta.responder(q.strip(), settings)
