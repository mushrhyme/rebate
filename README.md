# React Rebate v2 — PDF 청구서 분석 파이프라인

PDF 청구서 → SAP Excel 파이프라인. Azure OCR + Claude long-context 기반.

## 시스템 개요

|             | 기존                 | 현재                            |
| ----------- | -------------------- | ------------------------------- |
| 분석 단위   | 페이지 단위 RAG      | PDF 통째 long-context           |
| 양식별 로직 | Python 후처리 코드   | 정의 MD + JSON 룰 (데이터 주도) |
| 신규 양식   | 개발자가 정답지 작성 | 자동 정의서 생성 + 사용자 검토  |
| 산수        | LLM                  | Python 결정적 코드              |

- [아키텍처](docs/architecture.md) | [CLAUDE.md](CLAUDE.md) | [사용자 가이드](MANUAL.md)

---

## 환경 설정

### 사전 요건

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — Python 패키지 매니저
- [Node.js](https://nodejs.org/) 18+ — 프론트엔드 빌드
- PostgreSQL 14+ — 데이터베이스

### 1. 저장소 복제

```bash
git clone https://github.com/mushrhyme/rebate.git
cd rebate
```

### 2. Python 환경 (uv)

```bash
# Python 3.11 가상환경 생성 + 패키지 설치 (한 번에)
uv sync

# 이후 백엔드 실행
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

> uv가 없으면: `curl -LsSf https://astral.sh/uv/install.sh | sh`

### 3. 환경변수

```bash
cp backend/.env.example backend/.env
# backend/.env 를 열어 키 값 입력
```

| 변수 | 설명 |
|------|------|
| `DATABASE_URL` | PostgreSQL 연결 문자열 |
| `ANTHROPIC_API_KEY` | Anthropic API 키 |
| `AZURE_API_KEY` | Azure Document Intelligence 키 |
| `AZURE_API_ENDPOINT` | Azure 엔드포인트 URL |
| `WORKSPACE_ROOT` | 이 저장소의 절대 경로 |

### 4. 데이터베이스 초기화

```bash
psql -U postgres -c "CREATE DATABASE rebate_db_v2;"
psql -U postgres -d rebate_db_v2 -f backend/db/schema.sql
```

### 5. 프론트엔드

```bash
cd frontend
cp .env.example .env   # 필요 시 수정
npm install
npm run dev            # 개발 서버 (localhost:5173)
```

프론트엔드는 기본적으로 Vite 프록시를 통해 `localhost:8000`으로 API 요청을 포워딩합니다.

---

## 실행

터미널 두 개를 열어 각각 실행:

```bash
# 백엔드
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# 프론트엔드
cd frontend && npm run dev
```

브라우저에서 `http://localhost:5173` 접속.

---

## 디렉토리

```
backend/     Python 백엔드 (FastAPI + 파이프라인)
frontend/    React 프론트엔드 (Vite + TypeScript)
docs/        설계 문서 및 Phase 프롬프트
form_definitions/  양식별 기준 정보 MD
mappings/    소매처·제품 매핑 CSV
config/      form_types.json (Phase 4 설정)
scripts/     보조 스크립트
samples/     검증용 PDF
```
