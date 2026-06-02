# Form Definitions 인덱스

> Phase 2 시스템 프롬프트에 해당 `form_XX.md`를 포함시켜 항목 추출·조건 분류의 기준으로 사용한다.
> 같은 양식의 PDF를 묶어 처리할 때 시스템 프롬프트가 캐시 hit된다.

## 등록 양식

| form_id | 파일 |
|---------|------|
| form_01 | [form_01.md](form_01.md) |
| form_02 | (미등록) |
| form_03 | (미등록) |
| form_04 | [form_04.md](form_04.md) |
| form_05 | (미등록) |

## 양식 식별 방법

각 `form_*.md`의 `## 식별 패턴` 섹션에 기재된 문자열이 OCR 원문(`.ocr.txt`)에 **모두** 존재하면 해당 form으로 확정.
Phase 1 실행 전에 판별하므로 Phase 1 출력의 비결정성과 무관.
어느 form과도 매칭되지 않으면 → cold-start 분기 (사용자에게 확인 요청)

## cold-start

등록되지 않은 양식 → 사용자와 대화로 form 정의 초안 작성 → 확인 후 `form_XX.md`로 등록.
