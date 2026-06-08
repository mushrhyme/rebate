# 클라우드 전환 계획

> 작성일: 2026-06-08  
> 목표: PostgreSQL + 로컬 서버 구성 → AWS(서비스) + Google(데이터) 하이브리드 전환  
> 진행 상태: **G1 완료. G2·AWS 단계 대기 중.**

---

## 역할 분리 원칙

| 레이어 | 담당 | 이유 |
|--------|------|------|
| **서비스 실행** | AWS | 컴퓨팅·네트워크·스케일링 |
| **데이터 영구 보관** | Google Drive / Sheets | 현업이 브라우저에서 직접 접근 가능, Workspace 기존 구독 |
| **중간 산출물 (임시)** | S3 | Fargate와 같은 AWS 생태계, 처리 완료 후 삭제 |

---

## 목표 아키텍처

```
현업
  ↓ (G2, GWS 도입 후) PDF 업로드
Google Drive (rebate-inbox/)
      ↓ 감지
      ↓ ─────────────────────────── 또는 프론트 직접 업로드
사용자 브라우저
      ↓
CloudFront → S3 (React 빌드)
      ↓
API Gateway → Lambda (FastAPI)
                    ↓ PDF 접수 → Fargate task 트리거 (비동기)
                              ↓
                         ECS Fargate (Phase 1~4 파이프라인)
                              ├── Phase 1~3 중간 산출물 → S3 임시 저장
                              └── Phase 4 완료 시
                                    ├── 최종 결과 → Google Sheets    ← 현업 조회
                                    ├── PDF 원본  → Google Drive 보관 ← 현업 조회
                                    └── S3 임시 파일 삭제
```

---

## 저장소 역할 정의

| 데이터 | 저장소 | 접근 주체 | 생존 기간 |
|--------|--------|----------|----------|
| PDF 원본 | **Google Drive** | 현업 + 파이프라인 | 영구 |
| Phase 4 최종 결과 | **Google Sheets** | 현업 (조회·검토) | 영구 |
| CSV 매핑 마스터 | **Google Sheets** | 현업 (편집) + 파이프라인 (읽기) | 영구 |
| Phase 1~3 중간 산출물 | **S3 (임시)** | 파이프라인만 | 처리 완료 후 삭제 |
| React 빌드 | **S3 + CloudFront** | 브라우저 | 배포 시마다 갱신 |
| 문서 상태·매핑·리뷰 | **S3 JSON** | 백엔드 코드만 | 문서 수명 |

---

## 단계별 전환 계획

### ✅ Phase G1 — CSV 마스터 → Google Sheets 완료

- `ocr_retailer`, `ocr_product`, `ocr_dist`, `retail_user`, `unit_price`, `domae_retail_1` 탭 생성
- 백엔드 전체 (`mapping.py`, `phase3.py`, `phase3_dist_resolver.py`, `search.py`, `admin_retail.py`, `phase4_calc.py`) Sheets 우선 읽기/쓰기로 전환
- 로컬 CSV 6개 삭제 완료
- 스프레드시트 ID: `1UVoETT84o5LDiLEu9Xff4pAUulmXaajz6jvfgYDJenc`

---

### ⏭ Phase G2 — Google Drive 연동 (GWS 도입 후)

**목표**: PDF 원본 영구 보관 + Phase 4 결과 Sheets 저장 + Drive 입력 창구

**사전 조건**: 하반기 GWS 도입 후 진행

1. Drive 폴더 구조 생성
   ```
   rebate-inbox/      ← 현업 업로드 창구 (신규 PDF)
   rebate-archive/    ← 처리 완료 PDF 영구 보관
   ```

2. 파이프라인 완료 시 PDF → Drive `rebate-archive/` 이동

3. Phase 4 결과 → Sheets 신규 탭에 행 추가
   ```
   스프레드시트 탭: results
   컬럼: doc_id, 발행처, 발행월, 소매처코드, 소매처명, NET금액, 처리일시
   ```

4. Drive 폴더 감시 → 신규 파일 감지 시 파이프라인 자동 트리거 (Lambda scheduled)

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
- `backend/core/config.py` `aws_s3_bucket` + `jwt_secret` 필드 추가
- `backend/.env` JWT_SECRET + AWS_S3_BUCKET 추가
- `pyproject.toml` boto3 + PyJWT 의존성 추가
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

> Lambda 대신 EC2 t3.small 선택 이유: Docker 미설치 환경에서 pandas(73MB) + pymupdf(55MB) 등 파이프라인 의존성이 Lambda 250MB 제한을 초과할 수 있어, 전체 파이프라인 포함 단일 서버로 배포.

**EC2 서버**
- 인스턴스: `i-01248e65698af51d1` (t3.small, Amazon Linux 2023)
- Elastic IP: `54.116.122.115`
- DNS: `ec2-54-116-122-115.ap-northeast-2.compute.amazonaws.com`
- 포트: `8080` (uvicorn)
- 코드 배포: `s3://rebate-prod-590183751473/app-code/` → EC2 자동 다운로드
- 환경변수: `/app/backend/.env` (user data에서 생성)
- Google 인증: `/app/service_account.json` (S3에서 다운로드, OAuth 불필요)
- systemd: `rebate.service` (재시작 자동)

**CloudFront 업데이트**
- `/api/*` behavior → EC2 origin (포트 8080, HTTP)
- CachingDisabled + AllViewer 정책

**sheets_store.py 수정**
- `service_account.json` 우선 사용 (서버 배포용), 없으면 `~/.google-cli/token.pickle` fallback

**IAM 역할**: `rebate-ec2-role`
- S3 읽기/쓰기 (`rebate-prod-590183751473`)
- SSM Session Manager (비밀번호 없는 터미널 접속)

---

### ⏳ Phase D — 파이프라인 ECS Fargate (AWS 계정 확보 후)

**목표**: Phase 1~4 파이프라인 컨테이너화 (16장+ 문서 타임아웃 없음)

1. `Dockerfile` 작성
   ```dockerfile
   FROM python:3.11-slim
   COPY . /app
   WORKDIR /app
   RUN pip install -e .
   CMD ["python", "-m", "backend.pipeline.orchestrator"]
   ```
2. ECR에 이미지 푸시
3. ECS 클러스터 + 태스크 정의 (1 vCPU / 2GB)
4. 파이프라인 완료 시:
   - Phase 4 결과 → Google Sheets `results` 탭
   - PDF 원본 → Google Drive `rebate-archive/`
   - S3 `tmp/{doc_id}/` 삭제
5. Lambda에서 Fargate task 비동기 트리거

---

## 비용 예상

| 항목 | 비용 |
|------|------|
| Google Drive / Sheets | **$0** (Workspace 기존 구독) |
| S3 (임시 파일 위주, 회전 빠름) | ~$1/월 |
| CloudFront | 무료 티어 (1TB 이하) |
| Lambda | 무료 티어 (100만 요청 이하) |
| API Gateway | ~$1/100만 요청 |
| ECS Fargate (월 100건 × 3분) | ~$0.5/월 |
| **합계** | **~$2~3/월** |

> PostgreSQL RDS (db.t3.micro) ~$15/월 대비 절감. 영구 저장 비용은 Google이 흡수.

---

## 전체 진행 순서

```
✅ G1 완료 (Sheets 마스터)
✅ A  완료 (PostgreSQL 제거 — S3 JSON + JWT 전환)
✅ B  완료 (프론트엔드 S3 + CloudFront — dceaaeg5w4k3w.cloudfront.net)
✅ C  완료 (백엔드 EC2 t3.small — 54.116.122.115:8080)
⏭ G2 보류 (GWS 도입 후 — 하반기)
⏳ D  (파이프라인 ECS Fargate — Docker 설치 후)
```

---

## 리스크 및 주의사항

| 리스크 | 대응 |
|--------|------|
| Drive/Sheets API 장애 시 파이프라인 중단 | 로컬 fallback 로직 유지 (현재 구조) |
| S3 read-modify-write 동시성 | 내부 도구 1~2명 동시 사용 — 문제 시 DynamoDB 부분 도입 |
| Lambda cold start | Provisioned Concurrency 또는 warming 스케줄 |
| Fargate 네트워크 설정 | VPC/서브넷/보안그룹 사전 설계 필요 |
| Google OAuth 토큰 만료 (Fargate 환경) | 서비스 계정(Service Account) 방식으로 전환 필요 |
