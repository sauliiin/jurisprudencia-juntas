#!/usr/bin/env bash
# Atualiza o acervo público (votos organizados por lei/ato + pareceres) e
# publica no site: baixa só o que é novo, reexporta os JSONL do site, retreina a
# IA do site e envia (git add/commit/push) as mudanças em site_data/ e
# marco_atualizacao.json.
#
# Uso: ./atualizar_tudo.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

# Credenciais locais (SIF_USER, SIF_PASS) ficam no .env, fora do Git.
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
if [ -z "${SIF_USER:-}" ] || [ -z "${SIF_PASS:-}" ]; then
  echo "Defina SIF_USER e SIF_PASS no ambiente ou no arquivo .env." >&2
  exit 1
fi

PASTA_PUBLICA_VOTOS="1LW-tLhZfsc8l1-vBFqrFK86rtuUPIDer"

echo "== 1/5 Organizando votos por lei/ato (baixar_e_organizar_por_ato.py) =="
python3 -u baixar_e_organizar_por_ato.py --continuar

echo
echo "== 2/5 Atualizando acervo público de votos (preparar_acervo_publico.py) =="
python3 -u preparar_acervo_publico.py --buscar-novos

echo
echo "== 3/5 Atualizando links públicos do Drive (atualizar_links_drive_publico.py) =="
# Esse script retorna código 1 quando há arquivos ainda sem link público
# (normal: eles continuam pesquisáveis até serem sincronizados com a pasta
# pública). Não é motivo para abortar o restante da atualização.
set +e
python3 -u atualizar_links_drive_publico.py "$PASTA_PUBLICA_VOTOS"
status=$?
set -e
if [ "$status" -ne 0 ]; then
  echo "[aviso] atualizar_links_drive_publico.py terminou com pendências (arquivos sem link público). Seguindo mesmo assim."
fi

echo
echo "== 4/5 Atualizando pareceres (preparar_pareceres.py) =="
python3 -u preparar_pareceres.py

echo
echo "== 5/5 Retreinando a IA do site (treinar_ia.py) =="
# O modelo é refeito com o acervo atualizado. Se falhar (ex.: numpy ausente), o site continua
# funcionando com o modelo anterior; só os votos novos ficam fora dos "casos semelhantes".
set +e
python3 -u treinar_ia.py
status=$?
set -e
if [ "$status" -ne 0 ]; then
  echo "[aviso] treinar_ia.py falhou; mantendo o modelo anterior em site_data/ia/."
  git checkout -- site_data/ia 2>/dev/null || true
fi

echo
echo "== Publicando no Git =="
git add site_data/votos.jsonl site_data/pareceres.jsonl marco_atualizacao.json site_data/ia

if git diff --cached --quiet; then
  echo "Nada novo para publicar (nenhuma mudança em site_data/ ou marco_atualizacao.json)."
  exit 0
fi

git commit -m "Atualiza acervo público: votos e pareceres ($(date '+%Y-%m-%d %H:%M'))"
git push

echo
echo "Concluído: site atualizado e publicado."
