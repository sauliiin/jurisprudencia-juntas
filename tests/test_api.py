"""Rotas da API sobre o índice fictício da fixture."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from api.config import Settings
from api.logs import FormatoJson
from api.main import create_app


def ids(resposta) -> list[str]:
    return [item["file_id"] for item in resposta.json()["items"]]


# ---------- /health e /api/stats ----------


def test_health_ok(client: TestClient) -> None:
    resposta = client.get("/health")
    assert resposta.status_code == 200
    assert resposta.json() == {"status": "ok", "indice": True, "votos": 7, "versao": "1.0.0"}


def test_health_sem_indice(settings: Settings, tmp_path) -> None:
    app = create_app(settings.model_copy(update={"indice": tmp_path / "nao_existe.db"}))
    with TestClient(app) as cliente:
        resposta = cliente.get("/health")
        assert resposta.status_code == 503
        assert resposta.json()["status"] == "degradado"
        assert resposta.json()["indice"] is False
        # As rotas que dependem do índice respondem 503 padronizado.
        busca = cliente.get("/api/stats")
        assert busca.status_code == 503
        assert busca.json()["erro"]["codigo"] == "indisponivel"


def test_stats(client: TestClient) -> None:
    assert client.get("/api/stats").json() == {"votos": 7, "autos": 6, "pulados": 3}


# ---------- /api/search ----------


def test_busca_por_expressao(client: TestClient) -> None:
    resposta = client.get("/api/search", params={"q": "obra sem alvará"})
    dados = resposta.json()
    assert resposta.status_code == 200
    assert set(ids(resposta)) == {"voto1", "voto2", "voto7"}
    assert dados["total"] == 3
    assert all("<mark>" in item["trecho"] for item in dados["items"])


def test_expressao_exige_a_ordem(client: TestClient) -> None:
    # "alvará sem obra" não aparece como frase em nenhuma decisão.
    assert client.get("/api/search", params={"q": "alvará sem obra"}).json()["total"] == 0


def test_busca_por_palavras_e_sem_acento(client: TestClient) -> None:
    resposta = client.get("/api/search", params={"q": "alvara passeio", "modo": "words"})
    assert ids(resposta) == ["voto3"]


def test_busca_por_prefixo(client: TestClient) -> None:
    resposta = client.get("/api/search", params={"q": "regulariz", "modo": "words"})
    assert set(ids(resposta)) == {"voto2", "voto4"}


def test_palavras_reservadas_do_fts_nao_quebram(client: TestClient) -> None:
    resposta = client.get("/api/search", params={"q": "obra AND OR NOT", "modo": "words"})
    assert resposta.status_code == 200
    assert resposta.json()["total"] == 0


def test_consulta_fts_invalida_vira_400(client: TestClient, monkeypatch) -> None:
    # As expressões geradas são sempre válidas; isto cobre a defesa contra erro do FTS5.
    monkeypatch.setattr("api.services.busca.fts_query", lambda *_: "AND AND")
    resposta = client.get("/api/search", params={"q": "obra"})
    assert resposta.status_code == 400
    assert resposta.json()["erro"]["codigo"] == "requisicao_invalida"


def test_consulta_so_com_simbolos(client: TestClient) -> None:
    resposta = client.get("/api/search", params={"q": "?!", "modo": "words"})
    assert resposta.json() == {"items": [], "total": 0, "limit": 5, "offset": 0}


def test_filtro_de_instancia(client: TestClient) -> None:
    resposta = client.get("/api/search", params={"q": "obra sem alvará", "instancia": "2a_instancia"})
    assert ids(resposta) == ["voto7"]


def test_filtro_de_mes_e_ano(client: TestClient) -> None:
    resposta = client.get("/api/search", params={"mes": "março", "ano": "2025"})
    assert set(ids(resposta)) == {"voto1", "voto3", "voto7"}


def test_filtro_so_de_ano_com_consulta(client: TestClient) -> None:
    resposta = client.get("/api/search", params={"q": "passeio", "modo": "words", "ano": "2024"})
    assert ids(resposta) == ["voto4"]


def test_sem_consulta_lista_as_mais_recentes(client: TestClient) -> None:
    dados = client.get("/api/search", params={"limit": 3}).json()
    assert dados["total"] == 7
    assert [item["data"] for item in dados["items"]] == ["2025-04-05", "2025-03-30", "2025-03-20"]


def test_sem_consulta_com_instancia(client: TestClient) -> None:
    assert client.get("/api/search", params={"instancia": "1a_instancia"}).json()["total"] == 4


def test_paginacao(client: TestClient) -> None:
    primeira = client.get("/api/search", params={"limit": 2, "offset": 0}).json()
    segunda = client.get("/api/search", params={"limit": 2, "offset": 2}).json()
    assert (primeira["limit"], segunda["offset"]) == (2, 2)
    assert {i["file_id"] for i in primeira["items"]}.isdisjoint({i["file_id"] for i in segunda["items"]})


def test_limite_acima_do_maximo_usa_o_maximo(client: TestClient) -> None:
    dados = client.get("/api/search", params={"limit": 1000}).json()
    assert dados["limit"] == 5
    assert len(dados["items"]) == 5


def test_campos_do_item(client: TestClient) -> None:
    item = client.get("/api/search", params={"q": "executou obra"}).json()["items"][0]
    assert item["file_id"] == "voto1"
    assert item["data"] == "2025-03-10"
    assert item["resultado"] == "indeferido"
    assert item["ext"] == "pdf"
    assert item["file_url"] == "/api/file/voto1"
    assert item["drive_view_url"] == "https://drive.google.com/file/d/drivePublico1/view"
    assert item["autos"][0]["numero"] == "20250000001AI"


def test_respostas_nao_trazem_o_autuado(client: TestClient) -> None:
    corpo = client.get("/api/search", params={"q": "segunda instância"}).text
    assert "autuado" not in corpo
    assert "FULANO" not in corpo
    assert "autuado" not in client.get("/api/voto/voto7").text


@pytest.mark.parametrize(
    "params",
    [
        {"instancia": "3a_instancia"},
        {"modo": "semantic"},
        {"mes": "marco"},
        {"ano": "20x5"},
        {"limit": 0},
        {"offset": -1},
        {"q": "x" * 501},
    ],
)
def test_parametros_invalidos(client: TestClient, params: dict) -> None:
    resposta = client.get("/api/search", params=params)
    assert resposta.status_code == 422
    erro = resposta.json()["erro"]
    assert erro["codigo"] == "parametros_invalidos"
    assert erro["detalhes"][0]["campo"] == next(iter(params))
    assert erro["request_id"] == resposta.headers["x-request-id"]


# ---------- /api/voto ----------


def test_voto(client: TestClient) -> None:
    resposta = client.get("/api/voto/voto1")
    voto = resposta.json()
    assert resposta.status_code == 200
    assert voto["texto_preview"].startswith("O recorrente executou obra")
    assert voto["decisao_instancia"] == "Decisão de 1ª instância"
    assert voto["autos"][0]["lei"] == "LEI 9725/09"
    assert voto["drive_preview_url"].endswith("/drivePublico1/preview")


def test_voto_sem_link_publico(client: TestClient) -> None:
    voto = client.get("/api/voto/voto6").json()
    assert voto["drive_view_url"] is None
    assert voto["data"] is None
    assert voto["autos"] == []


def test_voto_inexistente(client: TestClient) -> None:
    resposta = client.get("/api/voto/nao-existe")
    assert resposta.status_code == 404
    assert resposta.json()["erro"] == {
        "codigo": "nao_encontrado",
        "mensagem": "Voto não encontrado",
        "detalhes": None,
        "request_id": resposta.headers["x-request-id"],
    }


# ---------- /api/file ----------


def test_arquivo_original(client: TestClient) -> None:
    resposta = client.get("/api/file/voto1")
    assert resposta.status_code == 200
    assert resposta.content == b"%PDF-1.4 teste"
    assert resposta.headers["content-disposition"].startswith("inline; filename*=UTF-8''voto1")


def test_arquivo_fora_da_pasta_nao_sai(client: TestClient) -> None:
    assert client.get("/api/file/voto2").status_code == 404


def test_arquivo_sem_caminho(client: TestClient) -> None:
    assert client.get("/api/file/voto3").status_code == 404


def test_arquivos_desligados_por_padrao(settings: Settings) -> None:
    with TestClient(create_app(settings.model_copy(update={"arquivos_dir": None}))) as cliente:
        resposta = cliente.get("/api/file/voto1")
        assert resposta.status_code == 404
        assert "não disponíveis" in resposta.json()["erro"]["mensagem"]


# ---------- Infraestrutura ----------


def test_cabecalhos(client: TestClient) -> None:
    resposta = client.get("/api/stats", headers={"X-Request-ID": "abc123"})
    assert resposta.headers["x-request-id"] == "abc123"
    assert resposta.headers["cache-control"] == "no-store"


def test_respostas_comprimidas(client: TestClient) -> None:
    resposta = client.get("/api/search", params={"limit": 5}, headers={"Accept-Encoding": "gzip"})
    assert resposta.headers["content-encoding"] == "gzip"


def test_cors_configurado(client: TestClient) -> None:
    resposta = client.get("/api/stats", headers={"Origin": "https://juris.omniai.com.br"})
    assert resposta.headers["access-control-allow-origin"] == "https://juris.omniai.com.br"
    outra = client.get("/api/stats", headers={"Origin": "https://outro.site"})
    assert "access-control-allow-origin" not in outra.headers


def test_site_estatico_e_redirecionamento(client: TestClient) -> None:
    raiz = client.get("/", follow_redirects=False)
    assert raiz.status_code == 307
    assert raiz.headers["location"] == "/site_publico/"
    assert "<title>site</title>" in client.get("/site_publico/").text
    assert client.get("/site_data/votos.jsonl").status_code == 200


def test_sem_site(settings: Settings) -> None:
    with TestClient(create_app(settings.model_copy(update={"servir_site": False}))) as cliente:
        assert cliente.get("/").status_code == 404
        assert cliente.get("/api/stats").status_code == 200


def test_documentacao(client: TestClient) -> None:
    esquema = client.get("/openapi.json").json()
    assert {"/health", "/api/stats", "/api/search", "/api/voto/{file_id}", "/api/file/{file_id}"} <= set(
        esquema["paths"]
    )
    assert client.get("/docs").status_code == 200


def test_rota_inexistente(client: TestClient) -> None:
    resposta = client.get("/api/nada")
    assert resposta.status_code == 404
    assert resposta.json()["erro"]["codigo"] == "nao_encontrado"


def test_erro_inesperado_vira_500_padronizado(settings: Settings) -> None:
    app = create_app(settings)

    @app.get("/explode")
    def explode() -> None:
        raise RuntimeError("falha")

    with TestClient(app, raise_server_exceptions=False) as cliente:
        resposta = cliente.get("/explode")
        assert resposta.status_code == 500
        assert resposta.json()["erro"]["codigo"] == "erro_interno"


def test_logs_estruturados(client: TestClient, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="api"):
        client.get("/api/stats", headers={"X-Request-ID": "log1"})
    registro = next(r for r in caplog.records if r.getMessage() == "requisicao")
    evento = json.loads(FormatoJson().format(registro))
    assert evento["rota"] == "/api/stats"
    assert evento["status"] == 200
    assert evento["metodo"] == "GET"
    assert evento["duracao_ms"] >= 0
