# React Rebate v2 — 리팩토링 기획

PDF 청구서 → SAP Excel 파이프라인을 RAG 기반에서 Claude long-context 기반으로 갈아끼우기 위한 설계 워크스페이스.

## 시작점

- [기존 시스템 스냅샷](docs/current-system.md) — 무엇이 있고 무엇을 바꾸려는가
- [새 아키텍처](docs/architecture.md) — Phase 1/2/3, 하이브리드 분담, SDK 선택, 의사결정 기록
- [신규 양식 자동 학습](docs/cold-start.md) — 이 리팩의 핵심 동기

## 핵심 변경

|             | 기존                 | 신규                            |
| ----------- | -------------------- | ------------------------------- |
| 분석 단위   | 페이지 단위 RAG      | PDF 통째 long-context           |
| 양식별 로직 | Python 후처리 코드   | 정의 MD + JSON 룰 (데이터 주도) |
| 신규 양식   | 개발자가 정답지 작성 | 자동 정의서 생성 + 사용자 검토  |
| 산수        | LLM                  | Python 결정적 코드              |
| 검색 인프라 | pgvector + BM25      | 없음 (long-context로 대체)      |

## 시스템 컨벤션

이 워크스페이스에서 Claude가 어떻게 작업하는지는 [CLAUDE.md](CLAUDE.md) 참조.

## 실행 명령어

uvicorn backend.main:app --host 0.0.0.0 --port 8001 --reload

## To Do List

1. 업로드 파일 경로 관리(현재 프로젝트 범위에 두어도 괜찮은지)
   - 사용자별/업로드일자별로 구분?
2. 계정별 소매처 조회 로직
3. 검토 체크 여부(1차/2차)
4. ocr_xxx.csv 업데이트 시점
5. 시스템이 틀렸을 때 어떻게 분기할지ㅈ
