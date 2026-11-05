"""Perguntas sobre o Entendimento das Juntas: parser, busca nos tópicos, citações e /api/perguntar."""

from __future__ import annotations

import io
import json
import sys
import types
import urllib.error
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from api import entendimento
from api.config import Settings
from api.main import create_app
from api.services import pergunta

PARAGRAFOS = [
    "VADE MECUM",
    "Jurisprudência é o conjunto das decisões.",
    "SUMÁRIO",
    "CAPÍTULO III - LIMPEZA URBANA 13",
    "1.4 – Autuação para roçar lote penhorado 17",
    "1.5 – “Lote” contíguo - Pg. 18",
    "CAPÍTULO III – LIMPEZA URBANA",
    "1 - Autuação por deixar de roçar ou limpar lote com frente para logradouro público",
    "O proprietário responde pela limpeza do lote.",
    "1.4 - Autuação para roçar lote penhorado",
    "A penhora não retira do proprietário o dever de roçar o lote.",
    "1.5 – “Lote” contíguo",
    "Cada lote contíguo recebe a sua notificação.",
    "CAPÍTULO VI - Temas de direito",
    "1 - Dupla visita.",
    "A dupla visita exige notificação antes do auto.",
    "7 - Órgão público pode ser autuado?",
    "Sim: órgão público responde pela infração como qualquer pessoa.",
    "8 - Duplicidade recursal " + "x" * 190 + " - O segundo recurso não é conhecido.",
]
TEXTO = "\n\n".join(PARAGRAFOS)
LINK = "https://drive.google.com/file/d/ent/view"
_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx(paragrafos: list[str], modificado: str | None = "2026-11-01T10:00:00Z") -> bytes:
    """.docx mínimo: um <w:p> por parágrafo; o primeiro leva um tab e uma caixa de texto."""
    corpo = "".join(f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragrafos[1:])
    caixa = "<w:r><w:txbxContent><w:p><w:r><w:t>na caixa</w:t></w:r></w:p></w:txbxContent></w:r>"
    primeiro = f"<w:p><w:r><w:t>{escape(paragrafos[0])}</w:t><w:tab/><w:t>fim</w:t></w:r>{caixa}</w:p>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f"<w:document {_NS}><w:body>{primeiro}{corpo}</w:body></w:document>")
        if modificado:
            z.writestr(
                "docProps/core.xml",
                '<cp:coreProperties xmlns:cp="c" xmlns:dcterms="http://purl.org/dc/terms/">'
                f"<dcterms:modified>{modificado}</dcterms:modified></cp:coreProperties>",
            )
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _limpa_caches():
    pergunta._carregar.cache_clear()
    pergunta._chave_cache.clear()
    yield
    pergunta._carregar.cache_clear()
    pergunta._chave_cache.clear()


@pytest.fixture
def ent_settings(settings: Settings, tmp_path: Path) -> Settings:
    """Settings com o entendimento.json no lugar padrão (dados_dir) e uma chave de teste."""
    doc = entendimento.documento(_docx(PARAGRAFOS), "ENTENDIMENTO.docx", LINK)
    (tmp_path / "entendimento.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return settings.model_copy(
        update={
            "entendimento": None,
            "dados_dir": tmp_path,
            "llm_api_key": SecretStr("chave-teste"),
            "llm_api_key_ssm": None,
        }
    )


class _Resposta(io.BytesIO):
    def __init__(self, corpo: bytes = b"", headers: dict | None = None):
        super().__init__(corpo)
        self.headers = headers or {}

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
    # O sumário fica de fora, com número de página solto ("... 17") ou "Pg. N".
    assert not any(t.endswith(("13", "17")) or "Pg." in t for t in titulos)
    assert "Pg. 18" not in " ".join(t["texto"] for t in topicos)
    assert (
        "CAPÍTULO III – LIMPEZA URBANA › 1 - Autuação por deixar de roçar ou limpar lote com frente para "
        "logradouro público › 1.4 - Autuação para roçar lote penhorado"
    ) in titulos
    assert any(t.endswith("1.5 - “Lote” contíguo") for t in titulos)  # título que começa com aspas
    assert "CAPÍTULO VI – TEMAS DE DIREITO › 1 - Dupla visita." in titulos
    # Título colado ao texto: corta no traço.
    duplicidade = next(t for t in topicos if "Duplicidade" in t["titulo"])
    assert duplicidade["texto"] == "O segundo recurso não é conhecido."


def test_linha_do_sumario_fora_do_sumario() -> None:
    topicos = entendimento.topicos(["CAPÍTULO I - Obras", "1 - Baixa - Pg. 4", "1 - Baixa", "Texto da baixa."])
    assert [t["titulo"] for t in topicos] == ["CAPÍTULO I – OBRAS › 1 - Baixa"]


def test_ler_docx() -> None:
    texto, modificado = entendimento.ler_docx(_docx(["Título", "Segundo parágrafo"]))
    assert entendimento.paragrafos(texto) == ["Título fim", "na caixa", "Segundo parágrafo"]
    assert modificado == "2026-11-01T10:00:00Z"
    assert entendimento.ler_docx(_docx(["Só"], modificado=None))[1] is None
    with pytest.raises(ValueError, match="docx"):
        entendimento.ler_docx(b"<html>login</html>")


def test_nome_do_arquivo_no_cabecalho() -> None:
    nome = entendimento._nome_do_cabecalho
    assert nome("attachment; filename*=UTF-8''ENTENDIMENTO%20vers%C3%A3o.docx") == "ENTENDIMENTO versão.docx"
    # O Drive manda UTF-8 cru; o urllib o lê como latin-1.
    assert nome('attachment; filename="vers\u00c3\u00a3o.docx"') == "versão.docx"
    assert nome('attachment; filename="custo €.docx"') == "custo €.docx"
    assert nome('attachment; filename="\u00e3.docx"') == "\u00e3.docx"
    assert nome("") is None


def test_data_http() -> None:
    assert entendimento._data_http("Thu, 05 Nov 2026 15:21:22 GMT") == "2026-11-05T15:21:22Z"
    assert entendimento._data_http(None) is None
    assert entendimento._data_http("ontem") is None


@pytest.fixture
def drive(monkeypatch: pytest.MonkeyPatch):
    """Troca o Drive: `drive.resposta` é o que volta; `drive.pedidos` guarda os pedidos."""
    estado = types.SimpleNamespace(pedidos=[], resposta=None)

    def urlopen(req, timeout):
        estado.pedidos.append(req)
        if isinstance(estado.resposta, Exception):
            raise estado.resposta
        return estado.resposta

    monkeypatch.setattr(entendimento.urllib.request, "urlopen", urlopen)
    return estado


def test_cli_baixa_do_drive(drive, tmp_path: Path, capsys) -> None:
    drive.resposta = _Resposta(
        _docx(PARAGRAFOS),
        {
            "Content-Disposition": 'attachment; filename="ENTENDIMENTO JUNTAS 2026 - vers\u00c3\u00a3o final.docx"',
            "Last-Modified": "Thu, 05 Nov 2026 15:21:22 GMT",
        },
    )
    saida = tmp_path / "site_data" / "entendimento.json"
    entendimento.main(["--drive-id", "abc", "--saida", str(saida)])
    doc = json.loads(saida.read_text(encoding="utf-8"))
    assert doc["arquivo"] == "ENTENDIMENTO JUNTAS 2026 - versão final.docx"
    assert doc["link"] == f"https://drive.google.com/file/d/{entendimento.PDF_ID}/view"  # o PDF, não o .docx
    assert doc["modificado_em"] == "2026-11-05T15:21:22Z"  # a data do Drive vale mais que a do arquivo
    assert doc["topicos"][1]["titulo"].startswith("CAPÍTULO III")
    assert "7 tópicos" in capsys.readouterr().out
    assert drive.pedidos[0].full_url.endswith("id=abc")
    assert drive.pedidos[0].get_header("User-agent").startswith("omniai-jurisprudencia/")


def test_cli_sem_nome_no_cabecalho(drive, tmp_path: Path) -> None:
    drive.resposta = _Resposta(_docx(PARAGRAFOS))
    saida = tmp_path / "entendimento.json"
    entendimento.main(["--saida", str(saida)])
    doc = json.loads(saida.read_text(encoding="utf-8"))
    assert doc["arquivo"] == f"{entendimento.DRIVE_ID}.docx"
    assert doc["modificado_em"] == "2026-11-01T10:00:00Z"


@pytest.mark.parametrize(
    "resposta",
    [_Resposta(b"<html>Fazer login</html>"), urllib.error.URLError("sem rede")],
    ids=["nao_compartilhado", "fora_do_ar"],
)
def test_cli_drive_falha(drive, tmp_path: Path, resposta) -> None:
    drive.resposta = resposta
    saida = tmp_path / "entendimento.json"
    with pytest.raises(SystemExit, match="Entendimento não lido"):
        entendimento.main(["--saida", str(saida)])
    assert not saida.exists()


def test_cli_docx_local(tmp_path: Path) -> None:
    arquivo = tmp_path / "ENTENDIMENTO.docx"
    arquivo.write_bytes(_docx(PARAGRAFOS))
    saida = tmp_path / "entendimento.json"
    entendimento.main(["--docx", str(arquivo), "--saida", str(saida)])
    assert json.loads(saida.read_text(encoding="utf-8"))["arquivo"] == "ENTENDIMENTO.docx"
    vazio = tmp_path / "VAZIO.docx"
    vazio.write_bytes(_docx([""]))
    with pytest.raises(SystemExit, match="nenhum tópico"):
        entendimento.main(["--docx", str(vazio), "--saida", str(saida)])


# ---------- Busca nos tópicos ----------


def test_termos_sem_acento_vazias_e_plural() -> None:
    assert pergunta.termos("Qual o prazo das notificações dos lotes?") == ["prazo", "notificacao", "lote"]
    assert pergunta.termos("Multas e órgãos legais") == ["multa", "orgao", "legal"]


def test_busca_acha_o_topico(ent_settings: Settings) -> None:
    doc = pergunta.carregar(ent_settings)
    achados = doc.buscar("Lote penhorado pode ser autuado por falta de roçada?")
    assert achados[0].titulo.endswith("1.4 - Autuação para roçar lote penhorado")
    assert doc.buscar("presidente da república") == []


def test_caminho_do_json_configurado(ent_settings: Settings, tmp_path: Path) -> None:
    outro = tmp_path / "build" / "entendimento.json"
    outro.parent.mkdir()
    outro.write_text(json.dumps({"topicos": [{"titulo": "Só no build", "texto": "tópico"}]}), encoding="utf-8")
    doc = pergunta.carregar(ent_settings.model_copy(update={"entendimento": outro}))
    assert [t.titulo for t in doc.topicos] == ["Só no build"]
    assert doc.arquivo == entendimento.NOME_PADRAO


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
    assert dados["documento"]["link"] == LINK
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
