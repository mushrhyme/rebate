---
name: gws
description: google-cli(google-workspace-cli)로 Gmail·Drive·Calendar·Sheets·Docs·Apps Script를 터미널에서 조작한다. 메일 검색/발송/회신/전달/라벨, Drive 파일 검색/업로드/다운로드/공유/삭제, 캘린더 일정 조회/생성, 시트 읽기/쓰기/추가, 문서 읽기/작성, Apps Script 코드 푸시 및 API 호출. Trigger, "/gws", "gws로 메일 보내", "구글 드라이브에서 찾아", "내 캘린더 일정", "구글 시트 읽어/써", "gmail 검색", "apps script", Google Workspace 작업 요청.
metadata:
  version: 1.0.0
  requires:
    bins:
      - google-cli
---

# gws — Google Workspace CLI

`google-cli`로 Google Workspace를 다룬다. 모든 결과는 stdout에 구조화 출력(JSON/text/table/csv), 에러는 stderr.
**이 파일 하나로 일반 작업은 끝난다.** 명령별 전체 플래그·예제·다단계 레시피는 필요할 때만 `references/`를 읽는다.

## 핵심 원칙 (토큰 효율)
- 아래 치트시트로 바로 실행한다. 플래그가 애매하면 그때만 `google-cli <그룹> <명령> --help`를 1줄 실행한다 — 문서를 다 읽지 말 것.
- **후속 처리가 필요하면 `-f json`**, 사용자에게 보여줄 땐 `-f text`/`table`/`csv`.
- ID(메시지·파일·시트·문서·스크립트)는 먼저 list/search로 얻은 뒤 후속 명령에 넘긴다.
- **발송·삭제·공유·쓰기 등 외부로 나가거나 되돌리기 어려운 작업은 실행 전 사용자에게 확인**한다.

## 실행 바이너리
- 기본은 `google-cli`(PATH에 설치된 경우). 설치: `uv tool install --editable <프로젝트경로>`.
- PATH에 없으면 프로젝트 venv 사용: `./venv/bin/google-cli` (이하 예제의 `google-cli`를 이걸로 대체).

## 인증
```bash
google-cli token-info            # 현재 토큰/스코프 확인 (먼저 점검)
google-cli login                 # 브라우저 OAuth (전체 서비스 스코프 1회 동의)
google-cli login --force         # 토큰 초기화 후 재동의 (스코프 변경 시)
google-cli logout                # 토큰 삭제
```
`insufficient scope`/권한 오류가 나면 `login --force`로 재로그인하라고 안내.

## 현재 사용 가능한 서비스
Gmail · Drive · Calendar · Sheets · Docs · Apps Script. (Chat은 Google Cloud의 Chat 앱 구성이 필요해 현재 환경에선 비활성.)

## 치트시트
대문자=위치인자(필수). `⚠`=쓰기/외부 작업(실행 전 확인). 자세한 건 `references/<서비스>.md`.

### Gmail  → `references/gmail.md`
```bash
google-cli gmail list [-q '검색쿼리'] [-l N] [-f json|text|table]   # 목록
google-cli gmail search QUERY [-l N]                                 # 검색 (list -q 와 동일 문법)
google-cli gmail triage [-m N]                                       # 안 읽은 받은편지함 요약
google-cli gmail get MESSAGE_ID        # 메타+스니펫
google-cli gmail read MESSAGE_ID       # 본문
google-cli gmail labels                # 라벨 목록
google-cli gmail send -t 받는사람 -s 제목 -b 본문 [--cc ..] [--bcc ..] [--html]   # 발송 ⚠
google-cli gmail reply MESSAGE_ID -b 본문 [--html]        # 회신 ⚠
google-cli gmail reply-all MESSAGE_ID -b 본문            # 전체회신 ⚠
google-cli gmail forward MESSAGE_ID -t 받는사람 [-b 덧붙일말]   # 전달 ⚠
google-cli gmail label MESSAGE_ID [-a 라벨] [-r 라벨]     # 메시지 라벨 추가/제거(ID 또는 이름) ⚠
```

### Drive  → `references/drive.md`
```bash
google-cli drive list [-q '쿼리'] [-l N]          # 목록 (쿼리: "name contains 'report'")
google-cli drive search KEYWORD [-t MIME] [-l N]  # 키워드 검색
google-cli drive get FILE_ID                      # 메타데이터
google-cli drive upload FILE_PATH [-n 이름] [-p 부모폴더ID]    # 업로드 ⚠
google-cli drive download FILE_ID [-o 출력경로 | -d 출력폴더]  # 다운로드
google-cli drive mkdir NAME [-p 부모폴더ID]       # 폴더 생성 ⚠
google-cli drive share FILE_ID -e 이메일 [-r reader|writer|owner] [--no-notify]  # 공유 ⚠
google-cli drive delete FILE_ID --yes             # 삭제 ⚠⚠
```

### Calendar  → `references/calendar.md`
```bash
google-cli calendar agenda [-d 일수]              # 다가오는 일정
google-cli calendar list-calendars               # 캘린더 목록
google-cli calendar insert -s 제목 --start ISO --end ISO [-d 설명] [-l 장소]  # 일정 생성(ISO 8601) ⚠
```

### Sheets  → `references/sheets.md`
```bash
google-cli sheets read SPREADSHEET_ID 'Sheet1!A1:C10' [-f json|text|table|csv]
google-cli sheets write SPREADSHEET_ID 'A1:B2' -v '[["a","b"],[1,2]]'   # values=JSON 2차원 ⚠
google-cli sheets append SPREADSHEET_ID 'A1' -v '값1,값2,값3'           # 한 행 추가(콤마) ⚠
google-cli sheets create TITLE        # 새 시트 ⚠
google-cli sheets info SPREADSHEET_ID # 메타데이터
```

### Docs  → `references/docs.md`
```bash
google-cli docs read DOCUMENT_ID       # 본문
google-cli docs info DOCUMENT_ID       # 메타데이터
google-cli docs write DOCUMENT_ID TEXT # 끝에 텍스트 추가 ⚠
google-cli docs create TITLE           # 새 문서 ⚠
```

### Apps Script  → `references/script.md`
```bash
google-cli script push SCRIPT_ID [-d ./src]    # 로컬 .gs/.js/.html/appsscript.json 전체 업로드 ⚠
google-cli script api --list                   # 호출 가능한 API 메서드 목록
google-cli script api METHOD -p KEY=VALUE [--body '{...}']   # 임의 API 호출
# 스크립트 프로젝트 목록은 Drive로: google-cli drive list -q "mimeType='application/vnd.google-apps.script'"
```
> ⚠ Apps Script `scripts.run`(코드 실행)은 스크립트의 GCP 프로젝트 연결이 필요해 현재 환경에선 동작하지 않는다.

## 범용 API 패스스루 (모든 서비스 공통)
전용 명령에 없는 기능은 `<서비스> api`로 공식 API 전체를 호출한다.
```bash
google-cli <서비스> api --list                 # 메서드 경로 전부
google-cli <서비스> api METHOD --describe       # 파라미터/바디 스키마
google-cli <서비스> api METHOD -p k=v --body '{...}' [-f json|yaml]
```
예) 라벨 생성: `google-cli gmail api users.labels.create -p userId=me --body '{"name":"프로젝트A"}'`

## 보안 규칙
- 토큰/자격증명을 출력하지 말 것.
- 쓰기/삭제(⚠ 표시) 명령은 **실행 전 사용자 확인**. 삭제는 특히 신중히.
- 외부로 나가는 메일/공유는 수신자·내용을 먼저 확인.

## 언제 references 를 읽나
- Gmail 검색 연산자, Sheets A1/values JSON, Calendar ISO 타임존 등 **포맷이 헷갈릴 때** → 해당 서비스 ref
- "메일 첨부를 드라이브에 저장", "시트 데이터로 메일 발송" 같은 **다단계 작업** → `references/recipes.md`
