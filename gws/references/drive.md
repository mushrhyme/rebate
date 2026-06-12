# drive — 상세 레퍼런스

> 전제: 메인 `../SKILL.md`의 인증·보안 규칙을 먼저 따른다. PATH 미설치 시 `google-cli` → `./venv/bin/google-cli`.

## 읽기 명령

### list — 파일 목록
```bash
google-cli drive list [-q '쿼리'] [-l N] [-f json|text|table]
```
| 플래그 | 설명 |
|------|------|
| `-q, --query` | Drive 쿼리 (아래 문법) |
| `-l, --limit` | 최대 결과 수 |
| `-f, --format` | json·text·table |

### search — 키워드 검색 (간편)
```bash
google-cli drive search KEYWORD [-t MIME] [-l N]
```
`-t, --type` MIME 필터 (예: `application/pdf`).

### get — 메타데이터
```bash
google-cli drive get FILE_ID [-f json|text]
```

## Drive 쿼리 문법 (`-q`)
| 목적 | 예시 |
|------|------|
| 이름 | `name contains 'report'`, `name = '2026예산.xlsx'` |
| 타입 | `mimeType = 'application/pdf'` |
| 폴더 | `mimeType = 'application/vnd.google-apps.folder'` |
| 스크립트 | `mimeType = 'application/vnd.google-apps.script'` |
| 부모 | `'FOLDER_ID' in parents` |
| 기간 | `modifiedTime > '2026-01-01T00:00:00'` |
| 휴지통 제외 | `trashed = false` |
| 조합 | `name contains '회의록' and mimeType = 'application/pdf' and trashed = false` |

자주 쓰는 MIME: Docs `application/vnd.google-apps.document`, Sheets `...spreadsheet`, Slides `...presentation`, 폴더 `...folder`.

## 쓰기/이동 명령 ⚠ (실행 전 사용자 확인)

### upload — 업로드
```bash
google-cli drive upload FILE_PATH [-n 드라이브이름] [-p 부모폴더ID]
```

### download — 다운로드 (읽기지만 로컬에 파일 생성)
```bash
google-cli drive download FILE_ID [-o 출력경로 | -d 출력폴더]
```

### mkdir — 폴더 생성
```bash
google-cli drive mkdir NAME [-p 부모폴더ID]
```

### share — 공유 ⚠ (외부로 권한 부여)
```bash
google-cli drive share FILE_ID -e 이메일 [-r reader|writer|owner] [--no-notify]
```
| 플래그 | 필수 | 설명 |
|------|----|------|
| `-e, --email` | ✓ | 공유 대상 이메일 |
| `-r, --role` | — | reader(기본)·writer·owner |
| `--no-notify` | — | 알림 메일 미발송 |

### delete — 삭제 ⚠⚠ (되돌리기 어려움)
```bash
google-cli drive delete FILE_ID --yes
```
`--yes` 없이는 확인 프롬프트. 실행 전 대상이 맞는지 반드시 확인.

## 자주 쓰는 조합
```bash
# 특정 폴더 안의 파일 나열
google-cli drive list -q "'FOLDER_ID' in parents and trashed = false" -f table
# 용량 큰 파일 찾기(메타 확인) — 정렬은 api 사용
google-cli drive api files.list -p q="trashed=false" -p orderBy="quotaBytesUsed desc" -p pageSize=10 -p fields="files(name,size,id)"
```

## See Also
- 메인 치트시트: `../SKILL.md`
- 다단계 레시피: `recipes.md`
