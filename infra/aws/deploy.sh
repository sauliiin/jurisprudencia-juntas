#!/usr/bin/env bash
# Publica certificado, stack da aplicação e site. Usado pelo GitHub Actions e também à mão
# (ex.: AWS CloudShell), com credenciais já configuradas e estas variáveis:
#   HOSTED_ZONE_ID, ARTIFACT_BUCKET  (saídas do stack omniailab-bootstrap)
#   ALERT_EMAIL (opcional), DOMAIN (padrão omniailab.com.br), AWS_REGION (padrão sa-east-1)
# Pré-requisito: build/lambda/ montado por infra/aws/build_lambda.sh.
set -euo pipefail
cd "$(dirname "$0")/../.."

: "${HOSTED_ZONE_ID:?defina HOSTED_ZONE_ID}" "${ARTIFACT_BUCKET:?defina ARTIFACT_BUCKET}"
DOMAIN="${DOMAIN:-omniailab.com.br}"
export AWS_REGION="${AWS_REGION:-sa-east-1}"

echo "== Certificado TLS (us-east-1)"
aws cloudformation deploy --region us-east-1 \
  --stack-name omniailab-certificado \
  --template-file infra/aws/certificado.yaml \
  --parameter-overrides DomainName="$DOMAIN" HostedZoneId="$HOSTED_ZONE_ID" \
  --no-fail-on-empty-changeset
CERT_ARN=$(aws cloudformation describe-stacks --region us-east-1 --stack-name omniailab-certificado \
  --query "Stacks[0].Outputs[?OutputKey=='CertificateArn'].OutputValue" --output text)

echo "== Stack do site e da API ($AWS_REGION)"
aws cloudformation package \
  --template-file infra/aws/template.yaml \
  --s3-bucket "$ARTIFACT_BUCKET" --s3-prefix lambda \
  --output-template-file build/packaged.yaml
aws cloudformation deploy \
  --stack-name omniailab-app \
  --template-file build/packaged.yaml \
  --capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND \
  --parameter-overrides DomainName="$DOMAIN" HostedZoneId="$HOSTED_ZONE_ID" \
    CertificateArn="$CERT_ARN" ${ALERT_EMAIL:+AlertEmail="$ALERT_EMAIL"} \
  --no-fail-on-empty-changeset
saida() {
  aws cloudformation describe-stacks --stack-name omniailab-app \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
BUCKET=$(saida SiteBucketName)
DISTRIBUTION=$(saida DistributionId)

echo "== Site para o S3 ($BUCKET)"
# Acervos grandes vão já comprimidos: o CloudFront só comprime arquivos de até 10 MB.
rm -rf build/site_data && mkdir -p build/site_data
cp -r site_data/. build/site_data/
find build/site_data -type f \( -name "*.jsonl" -o -name "*.json" -o -name "*.bin" \) \
  -exec gzip -9 -n {} \; -exec sh -c 'mv "$1.gz" "$1"' _ {} \;
aws s3 sync build/site_data/ "s3://$BUCKET/site_data/" --delete \
  --content-encoding gzip --cache-control "public, max-age=3600" \
  --exclude "*" --include "*.jsonl" --include "*.json" --include "*.bin"
aws s3 sync build/site_data/ "s3://$BUCKET/site_data/" --delete \
  --cache-control "public, max-age=3600" \
  --exclude "*.jsonl" --exclude "*.json" --exclude "*.bin"
aws s3 sync site_publico/ "s3://$BUCKET/site_publico/" --delete \
  --cache-control "public, max-age=300" --exclude "Autos/*.pdf"
aws s3 cp index.html "s3://$BUCKET/index.html" --cache-control "public, max-age=300"

echo "== Invalidação do CloudFront ($DISTRIBUTION)"
aws cloudfront create-invalidation --distribution-id "$DISTRIBUTION" --paths "/*" >/dev/null

echo "== Smoke test"
for _ in 1 2 3 4 5 6; do
  if curl -fsS "https://$DOMAIN/health"; then break; fi
  sleep 20
done
echo
curl -fsS "https://$DOMAIN/health" | grep -q '"status":"ok"'
curl -fsS -o /dev/null "https://$DOMAIN/site_publico/"
curl -fsS "https://$DOMAIN/api/search?q=alvar%C3%A1&limit=1" | grep -q '"total"'
echo "OK: https://$DOMAIN"
