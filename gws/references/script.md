# script — Apps Script 상세 레퍼런스

> 전제: 메인 `../SKILL.md`의 인증·보안 규칙을 먼저 따른다. PATH 미설치 시 `google-cli` → `./venv/bin/google-cli`.

Apps Script API에는 "프로젝트 목록" 메서드가 없다. 프로젝트는 Drive에 `application/vnd.google-apps.script` 타입으로 저장된다.

## 프로젝트 찾기 (Drive 경유)
```bash
google-cli drive list -q "mimeType='application/vnd.google-apps.script'" -l 50 -f json
```

## 코드 보기
```bash
google-cli script api projects.getContent -p scriptId=SCRIPT_ID -f json
```

## push — 로컬 파일 업로드 ⚠ (전체 교체)
```bash
google-cli script push SCRIPT_ID [-d ./src]
```
| 플래그 | 기본 | 설명 |
|------|----|------|
| `-d, --dir` | `.` | 스크립트 파일이 있는 디렉터리 |

- 매핑: `.gs`/`.js` → SERVER_JS, `.html` → HTML, `appsscript.json` → JSON(`appsscript`).
- 숨김 파일·`node_modules` 자동 제외, 하위 디렉터리 재귀.
- **프로젝트의 모든 파일을 교체**하므로 실행 전 확인. 먼저 `projects.getContent`로 백업 권장.

```bash
# 예: ./src 의 코드로 프로젝트 갱신
google-cli script push 1AbC...XyZ -d ./src
```

## api 패스스루 (버전·배포 등)
```bash
google-cli script api --list
google-cli script api projects.create --body '{"title":"새 프로젝트"}'
google-cli script api projects.versions.create -p scriptId=SID --body '{"description":"v1"}'
google-cli script api projects.deployments.create -p scriptId=SID \
  --body '{"versionNumber":1,"manifestFileName":"appsscript","description":"deploy"}'
```
> `script.deployments` 스코프가 없으면 배포 호출이 403. `login --force`로 재로그인하면 포함된다.

## 실행(scripts.run) 제약 ⚠
`scripts.run`(코드를 API로 실행)은 **스크립트의 GCP 프로젝트와 호출 OAuth 클라이언트의 프로젝트가 동일**해야 한다. 기본(자동 생성) GCP 프로젝트를 쓰는 스크립트는 403/404가 난다.
- 콘솔 접근/권한이 없는 환경에서는 API 실행 대신 **Apps Script 편집기에서 직접 실행** 또는 **시간 기반 트리거**를 사용한다.
- 편집기 URL: `https://script.google.com/d/SCRIPT_ID/edit`

## See Also
- 메인 치트시트: `../SKILL.md`
- 프로젝트 찾기는 `drive.md`의 스크립트 MIME 쿼리 참고
