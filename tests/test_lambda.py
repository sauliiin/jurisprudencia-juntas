"""Entrada da API no AWS Lambda: evento do API Gateway (HTTP API, payload 2.0) via Mangum."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from api.config import get_settings


def _evento(caminho: str, query: str = "") -> dict:
    return {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": caminho,
        "rawQueryString": query,
        "headers": {"host": "omniailab.com.br"},
        "requestContext": {
            "http": {"method": "GET", "path": caminho, "sourceIp": "203.0.113.1", "protocol": "HTTP/1.1"},
            "stage": "$default",
            "requestId": "teste",
            "domainName": "omniailab.com.br",
        },
        "isBase64Encoded": False,
    }


@pytest.fixture
def handler(pasta_dados: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JURIS_INDICE", str(pasta_dados / "indice_publico.db"))
    monkeypatch.setenv("JURIS_SERVIR_SITE", "false")
    get_settings.cache_clear()
    import api.lambda_handler as modulo

    yield importlib.reload(modulo).handler
    get_settings.cache_clear()


def test_health_no_lambda(handler) -> None:
    resposta = handler(_evento("/health"), None)
    assert resposta["statusCode"] == 200
    assert json.loads(resposta["body"])["indice"] is True


def test_busca_no_lambda(handler) -> None:
    resposta = handler(_evento("/api/search", "q=alvar%C3%A1&modo=words"), None)
    assert resposta["statusCode"] == 200
    assert json.loads(resposta["body"])["total"] >= 1


def test_lambda_nao_serve_o_site(handler) -> None:
    # No Lambda o site vem do S3 pelo CloudFront; a API não monta /site_publico.
    assert handler(_evento("/site_publico/"), None)["statusCode"] == 404
