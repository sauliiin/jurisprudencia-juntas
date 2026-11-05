"""Perguntas sobre o Entendimento das Juntas: parser, busca nos tópicos, citações e /api/perguntar."""

from __future__ import annotations

import io
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api import entendimento
from api.config import Settings
from api.main import create_app
from api.services import pergunta

TEXTO = "\n\n".join(
    [
        "VADE MECUM",
        "Jurisprudência é o conjunto das decisões.",
        "SUMÁRIO",
        "1 - Autuação por deixar de roçar - Pg. 3",
        "CAPÍTULO III – LIMPEZA URBANA",
        "1 - Autuação por deixar de roçar ou limpar lote com frente para logradouro público",
        "O proprietário responde pela limpeza do lote.",
        "1.4 - Autuação para roçar lote penhorado",
        "A penhora não retira do proprietário o dever de roçar o lote.",
        "CAPÍTULO VI - Temas de direito",
        "1 - Dupla visita.",
        "A dupla visita exige notificação antes do auto.",
        "7 - Órgão público pode ser autuado?",
        "Sim: órgão público responde pela infração como qualquer pessoa.",
        "8 - Duplicidade recursal " + "x" * 190 + " - O segundo recurso não é conhecido.",
    ]
)


@pytest.fixture(autouse=True)
def _limpa_caches():
    pergunta._carregar.cache_clear()
    pergunta._chave_cache.clear()
    yield
    pergunta._carregar.cache_clear()
    pergunta._chave_cache.clear()


@pytest.fixture
def pareceres(tmp_path: Path) -> Path:
    caminho = tmp_path / "pareceres.jsonl"
    docs = [
        {"nome_arquivo": "PARECER 1.pdf", "texto": "Parecer sobre ENTENDIMENTO JUNTAS 2024.docx citado."},
        {"nome_arquivo": "ENTENDIMENTO JUNTAS.docx", "texto": "versão antiga"},
        {
            "nome_arquivo": entendimento.ARQUIVO,
            "texto": TEXTO,
            "drive_view_url": "https://drive.google.com/file/d/ent/view",
            "modificado_em": "2025-05-28T11:58:14.490Z",
        },
    ]
    caminho.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in docs) + "\n", encoding="utf-8")
    return caminho


@pytest.fixture
def ent_settings(settings: Settings, pareceres: Path) -> Settings:
    return settings.model_copy(
        update={
            "entendimento": pareceres.parent / "entendimento.json",
            "dados_dir": pareceres.parent,
            "llm_api_key": SecretStr("chave-teste"),
            "llm_api_key_ssm": None,
        }
    )


class _Resposta(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


@pytest.fixture
def modelo(monkeypatch: pytest.MonkeyPatch):
    """Troca o serviço de IA: `modelo.texto` é a resposta; `modelo.pedidos` guarda o que foi enviado."""
    estado = types.SimpleNamespace(texto="A penhora não afasta o dever de roçar [1].", erro=None, pedidos=[])

    def urlopen(req, timeout):
        estado.pedidos.append({"url": req.full_url, "headers": dict(req.headers), "corpo": json.loads(req.data)})
        if estado.erro:
            raise estado.erro
        corpo = {"choices": [{"message": {"content": estado.texto}}]}
        return _Resposta(json.dumps(corpo).encode())

    monkeypatch.setattr(pergunta.urllib.request, "urlopen", urlopen)
    return estado


def _perguntar(ent_settings: Settings, q: str):
    with TestClient(create_app(ent_settings)) as cliente:
        return cliente.get("/api/perguntar", params={"q": q})


# ---------- Leitura do documento ----------


def test_topicos_do_texto() -> None:
    topicos = entendimento.topicos(entendimento.paragrafos(TEXTO))
    titulos = [t["titulo"] for t in topicos]
    assert titulos[0] == "Apresentação"
    assert "Pg. 3" not in " ".join(t["texto"] for t in topicos)  # o sumário fica de fora
    assert (
        "CAPÍTULO III – LIMPEZA URBANA › 1 - Autuação por deixar de roçar ou limpar lote com frente para "
        "logradouro público › 1.4 - Autuação para roçar lote penhorado"
    ) in titulos
    assert "CAPÍTULO VI – TEMAS DE DIREITO › 1 - Dupla visita." in titulos
    # Título colado ao texto: corta no traço.
    duplicidade = next(t for t in topicos if "Duplicidade" in t["titulo"])
    assert duplicidade["texto"] == "O segundo recurso não é conhecido."


def test_do_acervo_acha_so_a_versao_2024(pareceres: Path) -> None:
    doc = entendimento.do_acervo(pareceres)
    assert doc["arquivo"] == entendimento.ARQUIVO
    assert doc["link"] == "https://drive.google.com/file/d/ent/view"
    assert len(doc["topicos"]) == 6
    assert entendimento.do_acervo(pareceres, "NAO EXISTE.docx") is None


def test_cli_grava_o_json(pareceres: Path, tmp_path: Path, capsys) -> None:
    saida = tmp_path / "build" / "entendimento.json"
    entendimento.main(["--pareceres", str(pareceres), "--saida", str(saida)])
    assert json.loads(saida.read_text(encoding="utf-8"))["topicos"][1]["titulo"].startswith("CAPÍTULO III")
    assert "6 tópicos" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        entendimento.main(["--pareceres", str(pareceres), "--saida", str(saida), "--arquivo", "X.docx"])


# ---------- Busca nos tópicos ----------


def test_termos_sem_acento_vazias_e_plural() -> None:
    assert pergunta.termos("Qual o prazo das notificações dos lotes?") == ["prazo", "notificacao", "lote"]
    assert pergunta.termos("Multas e órgãos legais") == ["multa", "orgao", "legal"]


def test_busca_acha_o_topico(ent_settings: Settings) -> None:
    doc = pergunta.carregar(ent_settings)
    achados = doc.buscar("Lote penhorado pode ser autuado por falta de roçada?")
    assert achados[0].titulo.endswith("1.4 - Autuação para roçar lote penhorado")
    assert doc.buscar("presidente da república") == []


def test_carrega_do_json_quando_existe(ent_settings: Settings, pareceres: Path) -> None:
    doc = entendimento.do_acervo(pareceres)
    doc["topicos"] = [{"titulo": "Só no JSON", "texto": "tópico gerado pelo build"}]
    ent_settings.entendimento.write_text(json.dumps(doc), encoding="utf-8")
    assert [t.titulo for t in pergunta.carregar(ent_settings).topicos] == ["Só no JSON"]


def test_recorte_mantem_os_paragrafos_da_pergunta() -> None:
    texto = "\n\n".join(["Introdução geral do tema.", "Fala de penhora do lote.", "Outro assunto qualquer."])
    assert pergunta.recorte(texto, "penhora", 1000) == texto
    assert pergunta.recorte(texto, "penhora do lote", 30) == "[…]\n\nFala de penhora do lote.\n\n[…]"
    # Nenhum parágrafo cabe: corta o primeiro.
    assert pergunta.recorte("palavra " * 50, "nada", 20).endswith("[…]")


def test_mensagens_levam_so_os_topicos_numerados() -> None:
    topicos = [pergunta.Topico("Tópico A", "texto a"), pergunta.Topico("Tópico B", "texto b")]
    msgs = pergunta.mensagens("Minha pergunta?", topicos, 1000)
    assert msgs[0]["role"] == "system"
    assert "[1] Tópico A\ntexto a" in msgs[1]["content"]
    assert "[2] Tópico B\ntexto b" in msgs[1]["content"]
    assert msgs[1]["content"].endswith("Pergunta: Minha pergunta?")


def test_conferir_citacoes() -> None:
    texto, mantidas = pergunta.conferir_citacoes("Vale [1]. Também [2, 7] e [9] .", 2)
    assert texto == "Vale [1]. Também [2] e."
    assert mantidas == 2


# ---------- /api/perguntar ----------


def test_pergunta_respondida_com_citacao(ent_settings: Settings, modelo) -> None:
    resposta = _perguntar(ent_settings, "Lote penhorado pode ser autuado?")
    dados = resposta.json()
    assert resposta.status_code == 200
    assert dados["resposta"] == "A penhora não afasta o dever de roçar [1]."
    assert dados["aviso"] is None
    assert dados["modelo"] == ent_settings.llm_modelo
    assert dados["fontes"][0]["n"] == 1
    assert dados["fontes"][0]["titulo"].endswith("lote penhorado")
    assert dados["documento"]["link"] == "https://drive.google.com/file/d/ent/view"
    pedido = modelo.pedidos[0]
    assert pedido["url"] == ent_settings.llm_url
    assert pedido["headers"]["Authorization"] == "Bearer chave-teste"
    assert pedido["headers"]["User-agent"].startswith("omniai-jurisprudencia/")
    assert pedido["corpo"]["model"] == ent_settings.llm_modelo
    assert "A penhora não retira do proprietário" in pedido["corpo"]["messages"][1]["content"]


def test_citacao_inventada_sai(ent_settings: Settings, modelo) -> None:
    modelo.texto = "<think>rascunho</think>Deve roçar [1][5]."
    dados = _perguntar(ent_settings, "lote penhorado").json()
    assert dados["resposta"] == "Deve roçar [1]."


def test_resposta_sem_citacao_tem_aviso(ent_settings: Settings, modelo) -> None:
    modelo.texto = "Deve roçar."
    assert _perguntar(ent_settings, "lote penhorado").json()["aviso"] == pergunta.AVISO_SEM_CITACAO


def test_entendimento_nao_trata(ent_settings: Settings, modelo) -> None:
    modelo.texto = pergunta.NAO_TRATA
    dados = _perguntar(ent_settings, "multa do lote penhorado")
    assert dados.json()["aviso"] == pergunta.AVISO_NAO_TRATA


def test_sem_topico_nao_chama_o_modelo(ent_settings: Settings, modelo) -> None:
    dados = _perguntar(ent_settings, "presidente da república").json()
    assert dados["fontes"] == []
    assert dados["resposta"] is None
    assert dados["aviso"] == pergunta.AVISO_SEM_TOPICO
    assert modelo.pedidos == []


@pytest.mark.parametrize(
    "erro",
    [
        urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None),
        urllib.error.URLError("sem rede"),
        TimeoutError(),
    ],
)
def test_modelo_fora_do_ar_devolve_os_topicos(ent_settings: Settings, modelo, erro) -> None:
    modelo.erro = erro
    dados = _perguntar(ent_settings, "lote penhorado").json()
    assert dados["resposta"] is None
    assert dados["aviso"] == pergunta.AVISO_SEM_MODELO
    assert dados["fontes"][0]["texto"].startswith("A penhora")


@pytest.mark.parametrize("texto", ["", "   "])
def test_resposta_vazia_do_modelo(ent_settings: Settings, modelo, texto) -> None:
    modelo.texto = texto
    assert _perguntar(ent_settings, "lote penhorado").json()["aviso"] == pergunta.AVISO_SEM_MODELO


def test_resposta_malformada_do_modelo(ent_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pergunta.urllib.request, "urlopen", lambda req, timeout: _Resposta(b'{"choices": []}'))
    assert _perguntar(ent_settings, "lote penhorado").json()["aviso"] == pergunta.AVISO_SEM_MODELO


def test_sem_chave_devolve_os_topicos(ent_settings: Settings, modelo) -> None:
    dados = _perguntar(ent_settings.model_copy(update={"llm_api_key": None}), "lote penhorado").json()
    assert dados["aviso"] == pergunta.AVISO_SEM_MODELO
    assert modelo.pedidos == []


def test_pergunta_curta_demais(ent_settings: Settings) -> None:
    resposta = _perguntar(ent_settings, "a")
    assert resposta.status_code == 422


def test_sem_entendimento_responde_503(ent_settings: Settings, tmp_path: Path) -> None:
    vazio = tmp_path / "vazio"
    vazio.mkdir()
    falta = ent_settings.model_copy(update={"entendimento": vazio / "x.json", "dados_dir": vazio})
    resposta = _perguntar(falta, "lote penhorado")
    assert resposta.status_code == 503
    assert resposta.json()["erro"]["mensagem"] == "Entendimento das Juntas indisponível"


# ---------- Chave no SSM (Lambda) ----------


def _boto3_falso(monkeypatch: pytest.MonkeyPatch, valor: str | None) -> list[str]:
    chamadas: list[str] = []

    class Ssm:
        def get_parameter(self, Name, WithDecryption):  # mesmos nomes da API da AWS
            chamadas.append(Name)
            assert WithDecryption is True
            if valor is None:
                raise RuntimeError("ParameterNotFound")
            return {"Parameter": {"Value": valor}}

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=lambda servico: Ssm()))
    return chamadas


def test_chave_do_ssm_lida_uma_vez(ent_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    chamadas = _boto3_falso(monkeypatch, "chave-ssm")
    cfg = ent_settings.model_copy(update={"llm_api_key": None, "llm_api_key_ssm": "/omniailab/llm-api-key"})
    assert pergunta.chave_da_api(cfg) == "chave-ssm"
    assert pergunta.chave_da_api(cfg) == "chave-ssm"
    assert chamadas == ["/omniailab/llm-api-key"]


def test_chave_do_ssm_inexistente(ent_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    _boto3_falso(monkeypatch, None)
    cfg = ent_settings.model_copy(update={"llm_api_key": None, "llm_api_key_ssm": "/omniailab/llm-api-key"})
    assert pergunta.chave_da_api(cfg) is None
