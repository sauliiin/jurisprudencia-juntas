"""Entrada da API no AWS Lambda, atrás do API Gateway (HTTP API) e do CloudFront.

O site estático fica no S3; aqui roda só a API. A configuração vem das variáveis JURIS_*
definidas no template (infra/aws/template.yaml): índice dentro do pacote e servir_site=false.
"""

from __future__ import annotations

from mangum import Mangum

from api.main import create_app

app = create_app()
handler = Mangum(app, lifespan="off")
