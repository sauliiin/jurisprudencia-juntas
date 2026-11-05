#!/usr/bin/env bash
# Monta build/lambda/: código da API, dependências para Lambda arm64 (Python 3.12), o índice público
# e os tópicos do Entendimento das Juntas (para /api/perguntar).
set -euo pipefail
cd "$(dirname "$0")/../.."
rm -rf build/lambda && mkdir -p build/lambda
python -m pip install --quiet --target build/lambda \
  --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 --only-binary=:all: \
  -r infra/aws/requirements-lambda.txt
cp -r api build/lambda/
[ -f indice_publico.db ] || python -m api.indice
cp indice_publico.db build/lambda/
# Tópicos do Entendimento: gerados por python -m api.entendimento e versionados em site_data/.
cp site_data/entendimento.json build/lambda/
find build/lambda -name "__pycache__" -type d -prune -exec rm -rf {} +
du -sh build/lambda
