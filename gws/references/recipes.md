# recipes — 다단계 워크플로우 (현재 사용 가능한 기능만)

> 전제: 메인 `../SKILL.md`의 인증·보안 규칙. PATH 미설치 시 `google-cli` → `./venv/bin/google-cli`.
> JSON 파싱에 `jq`를 쓴다. 쓰기/발송/공유/삭제 단계는 **실행 전 사용자 확인**.

서비스별 단일 명령 상세는 같은 폴더의 `gmail.md`·`drive.md`·`calendar.md`·`sheets.md`·`docs.md`·`script.md` 참고.

---

## 1) 메일 첨부를 Drive에 저장
첨부는 `gmail get`/`api`로 attachment를 받아야 한다(전용 다운로드 명령 없음 → api 사용).
```bash
# 1. 첨부 있는 메일 찾기
google-cli gmail list -q 'has:attachment newer_than:7d' -l 10 -f json
# 2. 메시지 상세에서 attachmentId/파일명 확인
google-cli gmail api users.messages.get -p userId=me -p id=MESSAGE_ID -p format=full
# 3. 첨부 본문(base64) 받기 → 로컬 저장 후 Drive 업로드
google-cli gmail api users.messages.attachments.get -p userId=me -p messageId=MSG -p id=ATTACH_ID -f json \
  | jq -r '.data.data' | base64 --decode > /tmp/file.pdf
google-cli drive upload /tmp/file.pdf -n "첨부.pdf" -p FOLDER_ID    # ⚠ 확인
```

## 2) 시트 데이터로 개인화 메일 발송
```bash
# 1. 시트에서 이름/이메일 읽기
google-cli sheets read SID 'Sheet1!A2:B100' -f json | jq -c '.data[]'
# 2. 각 행마다 발송 (예시 — 실제 발송 전 대상/내용 확인) ⚠
google-cli sheets read SID 'Sheet1!A2:B100' -f json \
  | jq -r '.data[] | @tsv' \
  | while IFS=$'\t' read name email; do
      google-cli gmail send -t "$email" -s "안내" -b "${name}님, 안녕하세요."
    done
```

## 3) 시트 내용으로 Docs 보고서 생성
```bash
DID=$(google-cli docs create "월간 보고서 $(date +%Y-%m)" -f json | jq -r '.data.documentId')   # ⚠
google-cli sheets read SID 'Sheet1!A1:D20' -f csv \
  | while IFS= read line; do google-cli docs write "$DID" "$line"; done
echo "문서: https://docs.google.com/document/d/$DID/edit"
```

## 4) Drive 파일 공유하고 링크를 메일로
```bash
google-cli drive share FILE_ID -e bob@x.com -r reader            # ⚠ 외부 공유
LINK=$(google-cli drive get FILE_ID -f json | jq -r '.data.webViewLink')
google-cli gmail send -t bob@x.com -s "파일 공유" -b "링크: $LINK"   # ⚠ 발송
```

## 5) 라벨 붙이고 보관 (받은편지함 정리)
```bash
# 조건에 맞는 메일에 라벨 추가 + INBOX 제거(=보관)
for id in $(google-cli gmail list -q 'from:newsletter@x.com' -l 50 -f json | jq -r '.data[].id'); do
  google-cli gmail label "$id" -a "뉴스레터" -r INBOX           # ⚠
done
```

## 6) 캘린더 주간 일정 → 텍스트 요약
```bash
google-cli calendar agenda -d 7 -f json \
  | jq -r '.data[] | "\(.start)  \(.summary)"'
```

## 7) Drive 폴더 일괄 다운로드
```bash
for id in $(google-cli drive list -q "'FOLDER_ID' in parents and trashed=false" -l 100 -f json | jq -r '.data[].id'); do
  google-cli drive download "$id" -d ./downloads
done
```

## 8) 로컬 코드로 Apps Script 갱신
```bash
google-cli script api projects.getContent -p scriptId=SID -f json > backup.json   # 백업
google-cli script push SID -d ./src                                                # ⚠ 전체 교체
```

---

## 출력 파싱 팁
- 성공 응답은 `{"status":"success","data":...,"metadata":...}` → `jq '.data'`.
- 에러는 `{"status":"error","error":{...}}` → 분기: `jq -e '.status=="success"'`.
- 목록형 데이터는 보통 `.data`가 배열 → `jq -r '.data[].id'`.

## 막힐 때
- `insufficient scope`/403 → `google-cli login --force` 재로그인.
- Chat 관련은 현재 환경에서 Chat 앱 구성 미비로 비활성.
- `scripts.run` 403/404 → GCP 프로젝트 연결 필요(편집기 실행/트리거로 우회). `script.md` 참고.
