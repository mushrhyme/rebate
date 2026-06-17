#!/bin/bash
# 백엔드 배포 — 로컬 → S3 app-code → EC2 pull → rebate 재시작
#
# 핵심: EC2 런타임에서 변경되는 파일(form_types.json, form_definitions/*.md)은
# 백엔드가 S3 미러(config/...)에 기록한다. 이 스크립트는 배포 전에 미러와
# 로컬을 비교해, EC2에서 자란 설정을 로컬 구버전이 덮어쓰는 사고를 차단한다.
#
# 사용법:
#   bash scripts/deploy_backend.sh                # 미러≠로컬이면 중단 (기본)
#   bash scripts/deploy_backend.sh --take-remote  # 미러를 로컬로 가져온 뒤 배포
#   bash scripts/deploy_backend.sh --force-local  # 로컬 우선 배포 (미러 무시 — 의도 확인 후)
set -e

BUCKET="rebate-prod-590183751473"
REGION="ap-northeast-2"
INSTANCE="i-01248e65698af51d1"
MODE="${1:-}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── 1) 미러 가드 ──────────────────────────────────────────────────────────────
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 미러 도달 가능성 먼저 확인 — fetch 실패(네트워크/권한)와 "차이 없음"을 구분한다.
# config/ 프리픽스에는 백엔드가 쓰는 users.json 등이 상존하므로, 접근 가능하면 ls가 성공한다.
if ! aws s3 ls "s3://$BUCKET/config/" --region "$REGION" >/dev/null 2>&1; then
  echo "⚠ S3 미러(config/)에 접근할 수 없습니다 (네트워크/권한 가능성)."
  echo "  미러를 확인하지 못하면 '차이 없음'과 구분되지 않아 EC2 편집분을 덮어쓸 위험이 있습니다."
  echo "  확인: aws s3 ls s3://$BUCKET/config/ --region $REGION"
  if [ "$MODE" != "--force-local" ]; then
    echo "  안전하게 중단합니다. 의도적으로 미러 확인 없이 배포하려면 --force-local 사용."
    exit 1
  fi
  echo "  (--force-local 이므로 미러 확인 생략하고 진행)"
fi

# 개별 파일은 미러에 아직 없을 수 있으므로(최초 배포) 부재는 허용한다 — 위 ls로 도달성은 이미 확인됨.
aws s3 cp "s3://$BUCKET/config/form_types.json" "$TMP/form_types.json" \
  --region "$REGION" >/dev/null 2>&1 || true
aws s3 sync "s3://$BUCKET/config/form_definitions/" "$TMP/form_definitions/" \
  --region "$REGION" >/dev/null 2>&1 || true

DIFFS=()
if [ -f "$TMP/form_types.json" ] && ! cmp -s "$TMP/form_types.json" "config/form_types.json"; then
  DIFFS+=("config/form_types.json")
fi
if [ -d "$TMP/form_definitions" ]; then
  for f in "$TMP"/form_definitions/*.md; do
    [ -f "$f" ] || continue
    base=$(basename "$f")
    if [ ! -f "form_definitions/$base" ] || ! cmp -s "$f" "form_definitions/$base"; then
      DIFFS+=("form_definitions/$base")
    fi
  done
fi

if [ ${#DIFFS[@]} -gt 0 ]; then
  echo "⚠ EC2 런타임 미러와 로컬이 다른 파일:"
  for d in "${DIFFS[@]}"; do echo "   - $d"; done
  case "$MODE" in
    --take-remote)
      echo "▶ --take-remote: 미러 → 로컬 반영 후 git 커밋하고 배포 진행"
      [ -f "$TMP/form_types.json" ] && cp "$TMP/form_types.json" "config/form_types.json"
      if [ -d "$TMP/form_definitions" ]; then
        cp "$TMP"/form_definitions/*.md form_definitions/ 2>/dev/null || true
      fi
      # git을 단일 출처로 유지 — 미러 채택분을 즉시 커밋한다.
      # (커밋을 빠뜨리면 다른 머신/새 clone에서 로컬이 다시 stale → 사고 재발)
      git add config/form_types.json form_definitions/*.md
      if git diff --cached --quiet; then
        echo "   (스테이지된 변경 없음 — 커밋 생략)"
      else
        git commit -m "chore: adopt EC2 runtime form edits from S3 mirror (deploy --take-remote)" \
          || { echo "✗ 커밋 실패 — git 상태 확인 후 재시도하세요. 배포 중단."; exit 1; }
        echo "   ✓ 미러 채택분 git 커밋 완료 (배포 후 push 권장)"
      fi
      ;;
    --force-local)
      echo "▶ --force-local 선택됨 — 아래 EC2 런타임 편집분이 로컬로 덮어써져 영구 삭제됩니다."
      echo "   (diff 왼쪽 = 미러/EC2 현재값, 오른쪽 = 로컬/배포본)"
      for d in "${DIFFS[@]}"; do
        echo "── $d ────────────────────────────────────────"
        if [ "$d" = "config/form_types.json" ]; then
          diff -u "$TMP/form_types.json" "config/form_types.json" || true
        else
          base=$(basename "$d")
          diff -u "$TMP/form_definitions/$base" "$d" 2>/dev/null || true
        fi
      done
      printf "정말 위 EC2 편집분을 버리고 로컬을 배포합니까? (yes 입력): "
      read -r CONFIRM
      if [ "$CONFIRM" != "yes" ]; then
        echo "중단합니다. (미러 채택은 --take-remote)"
        exit 1
      fi
      echo "▶ 확인됨 — 로컬 우선 배포 진행"
      ;;
    *)
      echo ""
      echo "중단합니다. 차이를 확인하세요:"
      echo "  diff $TMP/form_types.json config/form_types.json"
      echo "다시 실행: --take-remote (미러 채택) 또는 --force-local (로컬 채택)"
      exit 1
      ;;
  esac
else
  echo "✓ 미러 가드 통과 — EC2 런타임 변경분 없음"
fi

# ── 2) 로컬 → S3 app-code ─────────────────────────────────────────────────────
echo "▶ 로컬 → S3 app-code 동기화..."
aws s3 sync . "s3://$BUCKET/app-code/" \
  --exclude ".git/*" --exclude ".claude/*" --exclude "*.pyc" \
  --exclude "__pycache__/*" --exclude "*/__pycache__/*" \
  --exclude "extracted/*" --exclude "samples/*" \
  --exclude "service_account.json" --exclude "token.pickle" \
  --exclude "node_modules/*" --exclude "frontend/node_modules/*" \
  --exclude "frontend/dist/*" --exclude ".venv/*" \
  --exclude "gws/*" --exclude ".DS_Store" --exclude "*/.DS_Store" \
  --region "$REGION" --delete --only-show-errors

# ── 3) EC2 pull + 재시작 + health ────────────────────────────────────────────
echo "▶ EC2 pull + rebate 재시작 (SSM)..."
CMD_ID=$(aws ssm send-command \
  --instance-id "$INSTANCE" \
  --document-name "AWS-RunShellScript" \
  --parameters '{"commands":[
    "aws s3 sync s3://rebate-prod-590183751473/app-code/ /app/ --region ap-northeast-2 --exclude \".venv/*\" --exclude \"samples/*\" --exclude \"extracted/*\" --delete --only-show-errors",
    "printf \"[Service]\\nEnvironment=EXTRA_CORS_ORIGINS=https://dceaaeg5w4k3w.cloudfront.net\\n\" > /etc/systemd/system/rebate.service.d/cors.conf",
    "systemctl daemon-reload",
    "systemctl restart rebate",
    "sleep 6",
    "systemctl is-active rebate",
    "curl -s -o /dev/null -w \"health=%{http_code}\\n\" http://localhost:8080/health"
  ]}' \
  --region "$REGION" --query "Command.CommandId" --output text)

echo "   CommandId: $CMD_ID — 결과 대기..."
sleep 25
aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE" \
  --region "$REGION" --query "{status:Status,out:StandardOutputContent,err:StandardErrorContent}" --output json \
  | python3 -c "
import json, sys
r = json.load(sys.stdin)
print('SSM:', r['status'])
print(r['out'].strip().splitlines()[-2:] if r['out'] else '')
if r['err']: print('stderr:', r['err'][:300])
assert r['status'] == 'Success' and 'health=200' in r['out'], '배포 검증 실패'
print('✅ 백엔드 배포 완료')
"
