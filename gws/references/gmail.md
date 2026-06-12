# gmail — 상세 레퍼런스

> 전제: 메인 `../SKILL.md`의 인증·보안 규칙을 먼저 따른다. 모든 예제의 `google-cli`는 PATH 미설치 시 `./venv/bin/google-cli`로 대체.

## 읽기 명령

### list — 메시지 목록
```bash
google-cli gmail list [-q '쿼리'] [-l N] [-f json|text|table]
```
| 플래그 | 필수 | 설명 |
|------|----|------|
| `-q, --query` | — | Gmail 검색 쿼리 (예: `is:unread`) |
| `-l, --limit` | — | 최대 결과 수 |
| `-f, --format` | — | json·text·table |

### search — 검색 (list -q 와 동일 문법)
```bash
google-cli gmail search QUERY [-l N] [-f json|text|table]
```

### triage — 안 읽은 받은편지함 요약
```bash
google-cli gmail triage [-m N] [-f json|text|table]
```
`-m, --max` 최대 메시지 수. 발신자·제목·날짜·스니펫·ID를 요약.

### get / read — 단일 메시지
```bash
google-cli gmail get MESSAGE_ID    # 메타데이터 + 스니펫
google-cli gmail read MESSAGE_ID   # 본문(text/html) 추출
```

### labels — 라벨 목록
```bash
google-cli gmail labels [-f json|text|table]
```

## Gmail 검색 쿼리 문법 (`-q` / search QUERY)
| 연산자 | 예시 |
|------|------|
| 보낸사람/받는사람 | `from:alice@x.com`, `to:me` |
| 제목/본문 | `subject:invoice`, `"정확한 구절"` |
| 상태 | `is:unread`, `is:starred`, `is:important` |
| 위치 | `in:inbox`, `in:sent`, `label:업무` |
| 첨부 | `has:attachment`, `filename:pdf` |
| 기간 | `after:2026/01/01`, `before:2026/02/01`, `newer_than:7d` |
| 조합 | `from:alice is:unread has:attachment newer_than:30d` |

## 쓰기 명령 ⚠ (실행 전 사용자 확인)

### send — 발송
```bash
google-cli gmail send -t 받는사람 -s 제목 -b 본문 [--cc ..] [--bcc ..] [--html]
```
| 플래그 | 필수 | 설명 |
|------|----|------|
| `-t, --to` | ✓ | 받는사람 (쉼표로 여러 명) |
| `-s, --subject` | ✓ | 제목 |
| `-b, --body` | ✓ | 본문 (`--html` 시 HTML) |
| `--cc` / `--bcc` | — | 참조/숨은참조 (쉼표 구분) |
| `--html` | — | 본문을 HTML로 |

### reply / reply-all — 회신
```bash
google-cli gmail reply MESSAGE_ID -b 본문 [--html]
google-cli gmail reply-all MESSAGE_ID -b 본문 [--html]
```
스레딩(In-Reply-To/References)은 자동 처리.

### forward — 전달
```bash
google-cli gmail forward MESSAGE_ID -t 받는사람 [-b 덧붙일말]
```

### label — 메시지에 라벨 추가/제거
```bash
google-cli gmail label MESSAGE_ID [-a 라벨] [-r 라벨] ...
```
- `-a, --add` / `-r, --remove` 는 **반복 지정 가능**, 값은 **라벨 ID 또는 이름** 둘 다 가능(내부에서 이름→ID 변환).
- 시스템 라벨 활용: 읽음 처리 `-r UNREAD`, 보관 `-r INBOX`, 별표 `-a STARRED`.
- 모르는 라벨 이름을 주면 `Unknown label` 에러로 차단됨.

## api 패스스루로만 되는 것 (전용 명령 없음)
```bash
# 라벨 생성 / 수정 / 삭제
google-cli gmail api users.labels.create -p userId=me --body '{"name":"프로젝트A","labelListVisibility":"labelShow","messageListVisibility":"show"}'
google-cli gmail api users.labels.delete -p userId=me -p id=Label_123
# 초안, 첨부 다운로드, 설정 등도 api 로 접근
google-cli gmail api --list
```

## 자주 쓰는 조합
```bash
# 안 읽은 첨부 메일 ID만 뽑기
google-cli gmail list -q 'is:unread has:attachment' -l 20 -f json
# 특정 발신자 최근 메일 본문 읽기
ID=$(google-cli gmail search 'from:boss@x.com newer_than:7d' -l 1 -f json | jq -r '.data[0].id')
google-cli gmail read "$ID"
```

## See Also
- 메인 치트시트: `../SKILL.md`
- 다단계 레시피(첨부 저장 등): `recipes.md`
