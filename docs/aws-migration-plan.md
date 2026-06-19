# 클라우드 전환 계획

> ✅ **완료된 마이그레이션 기록 (2026-06-19 확인):** S3+CloudFront 프론트 + EC2 백엔드로 전환 완료, 운영 중. D(Fargate)는 비활성화 결정으로 종료. 이 문서는 더 이상 실행할 작업 목록이 아니라 **전환 의사결정 기록**이다.

> 작성일: 2026-06-08  
> 최종 수정: 2026-06-09  
> 목표: PostgreSQL + 로컬 서버 구성 → AWS(서비스) + Google(데이터) 하이브리드 전환  
> 진행 상태: **G1·G2·A·B·C 운영 중. D(Fargate) 비활성화 — EC2 직접 실행으로 전환.**

---

## 역할 분리 원칙

| 레이어 | 담당 | 이유 |
|--------|------|------|
| **서비스 실행** | AWS | 컴퓨팅·네트워크·스케일링 |
| **데이터 영구 보관** | Google Drive / Sheets | 현업이 브라우저에서 직접 접근 가능, Workspace 기존 구독 |
| **중간 산출물 (임시)** | S3 | EC2와 같은 AWS 생태계, 처리 완료 후 삭제 가능 |

---

## 현재 아키텍처 (운영 중)

```
현업
  ↓ PDF 업로드 (브라우저)
사용자 브라우저
      ↓
CloudFront (dceaaeg5w4k3w.cloudfront.net)
  ├── /* → S3 (React 빌드)            ← 프론트엔드
  └── /api/* → EC2 :8080 (FastAPI)   ← 백엔드 API + 파이프라인
                    ↓ PDF 접수
              PDF → S3 + EC2 asyncio 백그라운드 태스크 (비동기)
                              ↓
                    EC2 직접 실행 (Phase 1~4 파이프라인)
                              ├── Phase 1~3 중간 산출물 → S3 (백그라운드 업로드)
                              └── Phase 4 완료 시
                                    ├── 최종 결과 → S3 JSON (조회·검토)
                                    ├── extracted → S3 백그라운드 업로드
                                    ├── PDF 원본  → Google Drive rebate-archive/  [운영 중]
                                    └── CSV 매핑 캐시 확정 → Google Sheets        [백그라운드]

Google Drive rebate-inbox/ 폴링  [운영 중 — DRIVE_INBOX_FOLDER_ID 활성]
```

---

## 저장소 역할 정의

| 데이터 | 저장소 | 접근 주체 | 생존 기간 |
|--------|--------|----------|----------|
| PDF 원본 | **Google Drive** (rebate-archive/) | 현업 + 파이프라인 | 영구 |
| Phase 4 최종 결과 | **S3 JSON** (`documents/{doc_id}/`) | 백엔드 + 프론트 | 문서 수명 |
| CSV 매핑 마스터 | **Google Sheets** | 현업 (편집) + 파이프라인 (읽기) | 영구 |
| Phase 1~3 중간 산출물 | **S3 (임시)** | 파이프라인만 | 처리 완료 후 삭제 가능 |
| React 빌드 | **S3 + CloudFront** | 브라우저 | 배포 시마다 갱신 |
| 문서 상태·매핑·리뷰 | **S3 JSON** | 백엔드 코드만 | 문서 수명 |

---

## 단계별 전환 계획

### ✅ Phase G1 — CSV 마스터 → Google Sheets 완료

- `ocr_retailer`, `ocr_product`, `ocr_dist`, `retail_user`, `unit_price`, `domae_retail_1` 탭 생성
- 백엔드 전체 (`mapping.py`, `phase3.py`, `phase3_dist_resolver.py`, `search.py`, `admin_retail.py`, `phase4_calc.py`) Sheets 우선 읽기/쓰기로 전환
- 로컬 CSV 6개 삭제 완료 (단, `mappings/unit_price.csv`는 EC2 로컬에 잔류)
- 스프레드시트 ID: `1UVoETT84o5LDiLEu9Xff4pAUulmXaajz6jvfgYDJenc`
- Google 인증: `token.pickle` 방식 — EC2 배포됨 (`/root/.google-cli/token.pickle`) ✅

**마이그레이션 후 발견·수정된 버그 (2026-06-08)**

| 버그 | 원인 | 수정 |
|------|------|------|
| `phase3.py:341` retail_user Sheets 미참조 | `.exists()` 가드가 Sheets 호출 전 단락 | `try: _read_csv()` except FileNotFoundError 패턴으로 교체 |
| `phase3_fallback.py` 동일 버그 | 동일 원인 | 동일 패턴으로 수정 |
| `_write_mapping_cache` (queries.py) 미호출 | S3 재작성 중 누락 | `confirm_mapping`에서 `_write_mapping_cache(doc_id=...)` 호출 복원 |
| `_write_mapping_cache` dist 컬럼 오류 | 2열로 기록 중 (ocr_name, code) | `phase3_output.json` 읽어 5열 정확히 기록: form_id, issuer_fp, rc, code, name |
| `_upsert_cache` (mappings.py) Sheets 미기록 | 로컬 CSV만 쓰고 Sheets 누락 | Sheets append_row 추가 |

**레거시 파일 삭제 완료**

| 삭제된 파일 | 이유 |
|------------|------|
| `backend/core/database.py` | asyncpg PostgreSQL 연결 풀 — 미사용 사문 코드 |
| `mappings/users_import.csv` | PostgreSQL import 전용 — 사문화 |
| `mappings/form_columns.json` | PostgreSQL DB 컬럼 매핑 — 사문화 |
| `scripts/import_users.py` | asyncpg 기반 PostgreSQL import 스크립트 |
| `scripts/upload_to_db.py` | psycopg2 기반 PostgreSQL upload 스크립트 |
| `rollout_baseline/` | Phase 3 Tool Use 롤아웃 실험 데이터 — Tool Use 운영 적용 완료로 불필요 |
| `pyproject.toml`: `asyncpg`, `psycopg2-binary` | PostgreSQL 드라이버 의존성 제거 |

---

### ✅ Phase G2 — Google Drive 연동 완료 (2026-06-09) [운영 중]

**목표**: PDF 원본 영구 보관 + Drive 입력 창구

**구현**
1. Drive 폴더 구조 (`python scripts/setup_g2.py` 로 생성)
   ```
   rebate-inbox/      ← 현업 업로드 창구 (신규 PDF)
   rebate-archive/    ← 처리 완료 PDF 영구 보관
   ```

2. 파이프라인 완료 시 PDF → Drive `rebate-archive/` 자동 보관  
   (`drive_storage.py` `_archive_folder_id` 설정 시 archive 경로 사용)

3. Drive `rebate-inbox/` 폴링 → 신규 PDF 감지 시 파이프라인 자동 트리거  
   (`backend/core/inbox_poller.py`: 300초 간격, `DRIVE_INBOX_FOLDER_ID` 설정 시 lifespan에서 자동 시작)  
   처리 완료 file_id: `s3://rebate-prod-*/config/drive_inbox_processed.json`

**EC2 .env 설정값 (운영 중)**
```bash
DRIVE_INBOX_FOLDER_ID=1-dFd8Rw0PyVwHjxJozB33Gp0MtU4eKza
DRIVE_ARCHIVE_FOLDER_ID=1WQ-cCvF3wHnrPA3Erw0XZ2EmKPAq_0uw
```

> **현재 상태**: EC2에서 정상 운영 중. inbox 폴러 Drive 폴더 ID로 동작 확인.  
> token.pickle Drive 스코프 포함 확인 — 만료 시 refresh_token으로 자동 갱신.  
> Drive 업로드 타이밍: 업로드 직후(PDF) → OCR 완료 후(pages) → Phase 4 완료 후(extracted)

---

### ✅ Phase A — S3 임시 저장소 + DB 제거 완료 (2026-06-08)

**S3 버킷**: `rebate-prod-590183751473` (ap-northeast-2)

**S3 구조**
```
s3://rebate-prod-590183751473/
  config/users.json                  ← 사용자 목록 (38명 마이그레이션 완료)
  config/form_edit_logs/{form_id}.json ← 양식 편집 이력 (S3로 이전)
  documents/{doc_id}/meta.json       ← 문서 상태·에러·토큰·run_id (28건 마이그레이션)
  documents/{doc_id}/mappings.json   ← 매핑 확정/미확정
  documents/{doc_id}/reviews.json    ← 리뷰
```

**완료 작업**
- `backend/core/s3_store.py` 신규 생성 (S3 JSON 읽기/쓰기 헬퍼)
- `backend/db/queries.py` 전면 재작성 → S3 boto3 기반
- `backend/core/auth.py` JWT 검증으로 교체 (PostgreSQL session 제거)
- `backend/api/routes/auth.py` JWT 발급 + S3 users.json CRUD
- `backend/api/routes/documents.py` DB 쿼리 제거
- `backend/api/routes/sap.py` · `usage.py` · `forms.py` · `form_manage.py` DB 제거
- `backend/pipeline/orchestrator.py` · `backend/core/stall_guard.py` DB 제거
- `backend/main.py` PostgreSQL 초기화 코드 제거
- `backend/core/config.py` `database_url` 필드 제거, `aws_s3_bucket` + `jwt_secret` 필드 추가
- `backend/.env` JWT_SECRET + AWS_S3_BUCKET 추가
- `pyproject.toml` boto3 + PyJWT 의존성 추가, asyncpg + psycopg2-binary 제거
- PostgreSQL 사용자 38명 → S3 `config/users.json` 마이그레이션
- PostgreSQL 문서 28건 (meta/mappings/reviews) → S3 마이그레이션

---

### ✅ Phase B — 프론트엔드 S3 + CloudFront 완료 (2026-06-08)

- S3 버킷: `rebate-frontend-590183751473` (ap-northeast-2)
- CloudFront 배포: `dceaaeg5w4k3w.cloudfront.net` (배포 ID: EYR2FQX9B5D5I)
- SPA 라우팅: 403/404 → `/index.html`
- `VITE_API_URL=''` (비어 있음) → CloudFront가 `/api/*`를 EC2로 프록시
- 빌드: `npm run build` (TypeScript 에러 5건 수정 포함)

---

### ✅ Phase C — 백엔드 EC2 + CloudFront 프록시 완료 (2026-06-08)

> Lambda 대신 EC2 t3.small 선택 이유: pandas(73MB) + pymupdf(55MB) 등 파이프라인 의존성이 Lambda 250MB 제한 초과 위험. 전체 파이프라인 포함 단일 서버로 배포.

**EC2 서버**
- 인스턴스: `i-01248e65698af51d1` (t3.small, Amazon Linux 2023)
- Elastic IP: `54.116.122.115`
- DNS: `ec2-54-116-122-115.ap-northeast-2.compute.amazonaws.com`
- 포트: `8080` (uvicorn)
- 코드 배포: `s3://rebate-prod-590183751473/app-code/` → EC2 자동 다운로드
- 환경변수: `/app/backend/.env` (user data에서 생성)
- Google 인증: `token.pickle` 방식 (google-workspace-cli OAuth)  
  경로: `/root/.google-cli/token.pickle` — **현재 배포됨** ✅  
  갱신/재배포 명령:
  ```bash
  TOKEN_B64=$(base64 -i ~/.google-cli/token.pickle)
  aws ssm send-command \
    --instance-id i-01248e65698af51d1 \
    --document-name "AWS-RunShellScript" \
    --parameters "{\"commands\":[\"mkdir -p /root/.google-cli && echo '${TOKEN_B64}' | base64 -d > /root/.google-cli/token.pickle\"]}" \
    --region ap-northeast-2
  ```
- systemd: `rebate.service` (재시작 자동)

**CloudFront 업데이트**
- `/api/*` behavior → EC2 origin (포트 8080, HTTP)
- CachingDisabled + AllViewer 정책

**IAM 역할**: `rebate-ec2-role`
- S3 읽기/쓰기 (`rebate-prod-590183751473`)
- SSM Session Manager (비밀번호 없는 터미널 접속)
- ECS RunTask (Fargate 트리거용)

**EC2 운영 주의사항**

> ⚠️ EC2 코드 동기화 시 아래 세 가지 **반드시** 제외

```bash
# S3 → EC2 동기화 (올바른 명령)
aws s3 sync s3://rebate-prod-590183751473/app-code/ /app/ \
  --region ap-northeast-2 \
  --exclude ".venv/*" \
  --exclude "samples/*" \
  --exclude "extracted/*" \
  --delete

# .venv 삭제된 경우 복구
rm -rf /app/.venv
cd /app && uv sync --python /usr/bin/python3.11
sudo systemctl restart rebate
```

| 제외 대상 | 이유 |
|----------|------|
| `.venv/*` | S3에 없음 → 삭제되면 uvicorn 실행 불가 |
| `samples/*` | 처리 중 PDF·OCR 파일 → 삭제되면 재분석 불가 |
| `extracted/*` | Phase 1~4 중간 산출물 → 삭제되면 재분석 필요 |

실제 사고: 2026-06-08 배포 시 `samples/`·`extracted/*` 미제외로 처리 중 파일 삭제 발생.

---

### ⛔ Phase D — 파이프라인 ECS Fargate (2026-06-08 구축 → 2026-06-09 비활성화)

**목표**: Phase 1~4 파이프라인 컨테이너화 (16장+ 문서 타임아웃 없음)

**비활성화 이유**: Fargate cold start 30-60초로 3장짜리 문서도 1분 소요. 파이프라인이 I/O 바운드 워크로드라 EC2 t3.small(2GB)에서 직접 실행해도 메모리·CPU 여유가 충분하다고 판단. EC2가 이미 24/7 운영 중이므로 Fargate 추가 비용도 불필요.

**AWS 인프라는 유지** (ECR 이미지, ECS 클러스터, IAM 역할) — 사용자 수가 크게 늘거나 EC2가 실제로 버거워지면 재활성화 가능.

**재활성화하려면** EC2 `.env`에 아래 추가 후 서비스 재시작:
```
ECS_CLUSTER_NAME=rebate-cluster
FARGATE_TASK_DEFINITION=rebate-pipeline
FARGATE_SUBNET_IDS=subnet-00e058b8cd8e5fd55,subnet-055926052401420ea,subnet-047be83815ed7c442,subnet-027876a8af051dfa4
FARGATE_SECURITY_GROUP_ID=sg-0ebedb4c2e51f2200
```

**ECR 이미지**: `590183751473.dkr.ecr.ap-northeast-2.amazonaws.com/rebate-pipeline:latest`  
**ECS 클러스터**: `rebate-cluster`  
**태스크 정의**: `rebate-pipeline` (1 vCPU / 2GB, Fargate)

**완료 작업**
- `Dockerfile` 작성 (python:3.11-slim, 249MB 압축)
- `.dockerignore` 작성
- `backend/pipeline/worker.py` 신규 — Fargate 워커 엔트리포인트 (`DOC_ID` env 기반 단일 문서 처리)
- `backend/core/s3_store.py` 파일 업로드/다운로드 헬퍼 추가 (`upload_file`, `download_file`, `upload_dir`, `download_dir`)
- `backend/core/config.py` ECS Fargate 설정 필드 추가 (`ecs_cluster_name`, `fargate_task_definition` 등)
- `backend/pipeline/orchestrator.py` S3 동기화 추가 — OCR 후 pages→S3, Phase 4 완료/pending 시 extracted→S3, resume 시 S3→로컬 복원
- `backend/api/routes/documents.py` — 업로드 시 PDF→S3, Fargate 트리거, EC2 fallback, pages/extracted S3 복원
- `scripts/setup_fargate.sh` 인프라 자동 설정 스크립트 (ECR, SSM, IAM 2개 역할, ECS 클러스터, 태스크 정의)
- IAM 역할: `rebate-fargate-execution-role` (ECR pull + SSM 읽기), `rebate-fargate-task-role` (S3 R/W)
- `rebate-ec2-role`에 ECS RunTask + S3 R/W 인라인 정책 추가
- SSM Parameter Store `/rebate/prod/*` 시크릿 저장
- EC2 재배포 완료 + `backend/.env` Fargate 설정 추가

**Docker 이미지 구조**

```
베이스: python:3.11-slim
포함:   backend/, scripts/, docs/, config/, form_definitions/, mappings/
제외:   .env, token.pickle, service_account.json, samples/, extracted/, tests/
CMD:    uvicorn backend.main:app  (기본 — EC2용)
워커:   python -m backend.pipeline.worker  (Fargate override)
```

**이미지 빌드 및 ECR 푸시**

```bash
# ECR 로그인
aws ecr get-login-password --region ap-northeast-2 \
  | docker login --username AWS --password-stdin \
    590183751473.dkr.ecr.ap-northeast-2.amazonaws.com

# 빌드 + 태그 + 푸시
docker build -t rebate-pipeline .
docker tag rebate-pipeline:latest \
  590183751473.dkr.ecr.ap-northeast-2.amazonaws.com/rebate-pipeline:latest
docker push \
  590183751473.dkr.ecr.ap-northeast-2.amazonaws.com/rebate-pipeline:latest
```

코드 변경 후 Fargate에서 반영하려면 반드시 이미지를 다시 빌드·푸시해야 함.  
EC2 배포(`s3 sync`)와 별개 — Fargate 이미지는 S3 sync로 자동 갱신되지 않음.

**Fargate 워커 (`backend/pipeline/worker.py`) 동작**

1. `DOC_ID` 환경변수 읽기 (없으면 종료)
2. S3 `config/token.pickle` → `/root/.google-cli/token.pickle` 다운로드 (Google 인증용, 없으면 skip)
3. S3 `documents/{doc_id}/original.pdf` → 로컬 다운로드
4. `run_pipeline()` 실행 (Phase 1~4)
5. 완료 후 컨테이너 종료 (extracted/ 파일은 orchestrator가 S3에 자동 업로드)

> **Fargate Google 인증 활성화 절차** (최초 1회 + token.pickle 갱신 시):
> ```bash
> # 1. token.pickle을 S3에 업로드
> aws s3 cp ~/.google-cli/token.pickle \
>   s3://rebate-prod-590183751473/config/token.pickle \
>   --region ap-northeast-2
>
> # 2. Docker 이미지 재빌드·푸시 (worker.py 코드 반영)
> docker build -t rebate-pipeline . && \
> docker tag rebate-pipeline:latest \
>   590183751473.dkr.ecr.ap-northeast-2.amazonaws.com/rebate-pipeline:latest && \
> docker push \
>   590183751473.dkr.ecr.ap-northeast-2.amazonaws.com/rebate-pipeline:latest
> ```

**트리거 버그 수정 (2026-06-08)**

| 버그 | 원인 | 수정 |
|------|------|------|
| 모든 Fargate 트리거 실패 | `DOC_ID`에 한글·일본어 포함 시 ECS RunTask가 `InvalidParameterException: Environment variable value must be normalized according to Unicode Normalization Form C` 반환 | `_trigger_fargate_task()`에 `unicodedata.normalize("NFC", doc_id)` 적용 (documents.py + inbox_poller.py) |

**트리거 방식**
```
PDF 업로드 → EC2 FastAPI → ECS RunTask → Fargate 컨테이너
                           ↓ 실패 시
                      EC2 백그라운드 태스크 (fallback)
```

**S3 데이터 흐름**
```
업로드 시: PDF → s3://rebate-prod-*/documents/{doc_id}/original.pdf
OCR 완료:  pages → s3://rebate-prod-*/documents/{doc_id}/pages/*
Phase 4 완료/pending: extracted → s3://rebate-prod-*/documents/{doc_id}/extracted/*
resume 시: EC2가 S3에서 extracted 자동 복원
```

---

## 비용 예상

| 항목 | 비용 |
|------|------|
| Google Drive / Sheets | **$0** (Workspace 기존 구독) |
| S3 (임시 파일 + JSON 문서 상태) | ~$1/월 |
| CloudFront | 무료 티어 (1TB 이하) |
| EC2 t3.small | ~$15/월 |
| ECS Fargate | $0 (비활성화) |
| **합계** | **~$16/월** |

> PostgreSQL RDS (db.t3.micro) ~$15/월 + EC2가 함께였다면 ~$30. 현재는 DB 비용 제거로 절반 수준.

---

## 전체 진행 순서

```
✅ G1 완료 (Sheets 마스터 — token.pickle EC2 배포 완료, 운영 중)
✅ A  완료 (PostgreSQL 제거 — S3 JSON + JWT 전환)
✅ B  완료 (프론트엔드 S3 + CloudFront — dceaaeg5w4k3w.cloudfront.net)
✅ C  완료 (백엔드 EC2 t3.small — 54.116.122.115:8080, 파이프라인 EC2 직접 실행)
⛔ D  비활성 (ECS Fargate — cold start 30-60초로 비활성화, 인프라는 유지, 2026-06-09)
✅ G2 완료 (Drive archive/inbox — EC2 운영 중, 2026-06-09)
```

---

## 리스크 및 주의사항

| 리스크 | 대응 |
|--------|------|
| Drive/Sheets API 장애 시 파이프라인 중단 | `get_sheets_store()` 초기화 실패 시 warning 후 로컬 CSV fallback. 로컬 CSV도 없으면 빈 리스트 반환 → 매핑 miss 발생 |
| S3 read-modify-write 동시성 | 내부 도구 1~2명 동시 사용 — 문제 시 DynamoDB 부분 도입 |
| EC2 sync `--delete` 로 운영 파일 삭제 | Phase C 운영 주의사항 참조 — `.venv/*`·`samples/*`·`extracted/*` 3개 제외 필수 |
| Google Sheets API 쓰기 할당량 | 매핑 확정 시 `append_row` 호출 — 현재 사용량 범위 내 |
| Fargate 트리거 한글 doc_id | NFC 정규화 누락 시 ECS 오류 — 2026-06-08 수정 완료 (현재 Fargate 비활성) |
