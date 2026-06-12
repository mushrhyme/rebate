# calendar — 상세 레퍼런스

> 전제: 메인 `../SKILL.md`의 인증·보안 규칙을 먼저 따른다. PATH 미설치 시 `google-cli` → `./venv/bin/google-cli`.

## 읽기 명령

### agenda — 다가오는 일정
```bash
google-cli calendar agenda [-d 일수] [-f json|text|table]
```
`-d, --days` 표시할 일수(기본값 적용). 시작시각·제목·상태를 요약.

### list-calendars — 캘린더 목록
```bash
google-cli calendar list-calendars [-f json|text|table]
```
캘린더 ID(후속 api 호출 시 calendarId로 사용) 확인용.

## 쓰기 명령 ⚠ (실행 전 사용자 확인)

### insert — 일정 생성
```bash
google-cli calendar insert -s 제목 --start ISO --end ISO [-d 설명] [-l 장소]
```
| 플래그 | 필수 | 설명 |
|------|----|------|
| `-s, --summary` | ✓ | 일정 제목 |
| `--start` | ✓ | 시작 (ISO 8601) |
| `--end` | ✓ | 종료 (ISO 8601) |
| `-d, --description` | — | 설명 |
| `-l, --location` | — | 장소 |

## ISO 8601 시각 포맷 (중요)
- 시각 지정: `2026-06-10T14:00:00+09:00` (KST는 `+09:00`).
- 타임존을 빼면 모호해지므로 **항상 오프셋 포함** 권장.
- 종일 일정 등 고급 옵션은 `calendar api events.insert --body '{...}'` 사용.

```bash
# 예: 6/10 14:00~15:00 KST 회의
google-cli calendar insert -s "주간 회의" --start 2026-06-10T14:00:00+09:00 --end 2026-06-10T15:00:00+09:00 -l "3층 회의실"
```

## api 패스스루로 되는 것 (전용 명령 없음)
```bash
google-cli calendar api --list
# 참석자 포함 생성 / 수정 / 삭제 / 반복일정
google-cli calendar api events.insert -p calendarId=primary \
  --body '{"summary":"미팅","start":{"dateTime":"2026-06-10T14:00:00+09:00"},"end":{"dateTime":"2026-06-10T15:00:00+09:00"},"attendees":[{"email":"a@x.com"}]}'
google-cli calendar api events.list -p calendarId=primary -p timeMin=2026-06-01T00:00:00+09:00 -p singleEvents=true -p orderBy=startTime
```

## See Also
- 메인 치트시트: `../SKILL.md`
- 다단계 레시피: `recipes.md`
