# Phase 4 실행

`$ARGUMENTS`에 doc_id가 전달된다. (예: `/phase4 sample_003`)

아래를 실행한다:

```bash
python3 scripts/phase4_calc.py --doc $ARGUMENTS --save
```

- `extracted/$ARGUMENTS/phase3_output.json` 이 없으면: "phase3_output.json이 없습니다. Phase 3를 먼저 실행해 주세요." 안내 후 중단.
- 실행 완료 후 `extracted/$ARGUMENTS/phase4_output.json` 내용을 요약 표시한다.
- 오류 발생 시 stderr를 그대로 표시한다.
