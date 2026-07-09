#!/usr/bin/env bash
# Atualiza o acervo público (votos organizados por lei/ato + pareceres) e
# publica no site: baixa só o que é novo, reexporta os JSONL do site e
# envia (git add/commit/push) as mudanças em site_data/ e marco_atualizacao.json.
#
# Uso: ./atualizar_tudo.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PASTA_PUBLICA_VOTOS="1LW-tLhZfsc8l1-vBFqrFK86rtuUPIDer"

echo "== 1/4 Organizando votos por lei/ato (baixar_e_organizar_por_ato.py) =="
python3 -u baixar_e_organizar_por_ato.py --continuar

echo
echo "== 2/4 Atualizando acervo público de votos (preparar_acervo_publico.py) =="
python3 -u preparar_acervo_publico.py --buscar-novos

echo
echo "== 3/4 Atualizando links públicos do Drive (atualizar_links_drive_publico.py) =="
python3 -u atualizar_links_drive_publico.py "$PASTA_PUBLICA_VOTOS"

echo
echo "== 4/4 Atualizando pareceres (preparar_pareceres.py) =="
python3 -u preparar_pareceres.py

echo
echo "== Publicando no Git =="
git add site_data/votos.jsonl site_data/pareceres.jsonl marco_atualizacao.json

if git diff --cached --quiet; then
  echo "Nada novo para publicar (nenhuma mudança em site_data/ ou marco_atualizacao.json)."
  exit 0
fi

git commit -m "Atualiza acervo público: votos e pareceres ($(date '+%Y-%m-%d %H:%M'))"
git push

echo
echo "Concluído: site atualizado e publicado."
