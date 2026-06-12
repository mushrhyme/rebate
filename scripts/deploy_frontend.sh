#!/bin/bash
# 프론트엔드 빌드 → S3 배포 → CloudFront 무효화
# 사용법: bash scripts/deploy_frontend.sh
set -e

BUCKET="rebate-frontend-590183751473"
CF_ID="EYR2FQX9B5D5I"
FRONTEND_DIR="$(dirname "$0")/../frontend"
REGION="ap-northeast-2"

echo "▶ 빌드..."
cd "$FRONTEND_DIR"
npm run build

echo "▶ assets/ 업로드 (장기 캐시)..."
aws s3 sync dist/assets/ "s3://$BUCKET/assets/" \
  --region "$REGION" \
  --cache-control "public, max-age=31536000, immutable" \
  --delete

echo "▶ 나머지 정적 파일 업로드..."
aws s3 sync dist/ "s3://$BUCKET/" \
  --region "$REGION" \
  --exclude "assets/*" \
  --delete

echo "▶ index.html — no-cache 적용..."
aws s3 cp dist/index.html "s3://$BUCKET/index.html" \
  --region "$REGION" \
  --content-type "text/html" \
  --cache-control "no-cache, no-store, must-revalidate" \
  --metadata-directive REPLACE

echo "▶ CloudFront 캐시 무효화 (index.html)..."
INVAL_ID=$(aws cloudfront create-invalidation \
  --distribution-id "$CF_ID" \
  --paths "/index.html" \
  --region us-east-1 \
  --query "Invalidation.Id" --output text)
echo "   무효화 ID: $INVAL_ID (보통 1-2분 소요)"

echo "✅ 배포 완료"
