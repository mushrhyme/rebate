# 설치 가이드 — macOS

Google Workspace CLI(`google-cli`)를 macOS에 설치하고 인증하는 방법입니다.

## 사전 준비
- Python 3.10+ 설치
- Google Cloud Console에서 발급한 OAuth 클라이언트 `credentials.json` 파일 (Desktop/installed 유형)

## 1. CLI 설치

PATH에 전역 설치(권장):

```bash
uv tool install --editable /path/to/google-cli
```

또는 프로젝트 가상환경 사용:

```bash
cd /path/to/google-cli
python -m venv venv
./venv/bin/pip install -e .
# 이후 예제의 `google-cli` 를 `./venv/bin/google-cli` 로 대체
```

## 2. 자격증명(credentials.json) 배치

CLI는 자격증명을 아래 순서로 찾습니다:

1. 명령에 `--credentials <경로>` 를 직접 지정한 경우 그 경로
2. **`~/.google-cli/credentials.json`** ← 기본·권장 위치
3. 현재 작업 폴더의 `./credentials.json`
4. 환경변수 `GOOGLE_CREDENTIALS` 에 지정한 경로

`~` 는 macOS에서 `/Users/<사용자>` 입니다. 따라서 기본 위치는:

```
/Users/<사용자>/.google-cli/credentials.json
```

다운로드한 `credentials.json` 이 `~/Downloads` 에 있다고 가정하고 배치:

```bash
# 설정 폴더 생성 (이미 있으면 무시)
mkdir -p ~/.google-cli

# 자격증명 복사
cp ~/Downloads/credentials.json ~/.google-cli/credentials.json

# 권한 600 (소유자만 읽기/쓰기 — 보안 권장)
chmod 600 ~/.google-cli/credentials.json
```

## 3. 로그인

```bash
google-cli login          # 브라우저 OAuth 동의 (전체 서비스 스코프 1회)
```

브라우저가 열리면 사용할 Google 계정으로 동의합니다.
토큰은 `~/.google-cli/token.pickle` 에 저장되며 자동 갱신됩니다.

## 4. 설치 확인

```bash
google-cli token-info     # 활성 자격증명 경로·계정·스코프 확인
google-cli gmail list -l 3
```

## 계정 / 클라이언트를 교체할 때

자격증명만 바꾸고 기존 토큰이 남아 있으면 **짝이 맞지 않아 로그인이 깨집니다.**
반드시 기존 토큰을 함께 정리하세요:

```bash
google-cli logout                       # 기존 token.pickle 삭제
cp ~/Downloads/새credentials.json ~/.google-cli/credentials.json
chmod 600 ~/.google-cli/credentials.json
google-cli login                        # 새 계정으로 재로그인
```

## 경로를 바꾸고 싶다면 (환경변수)

파일 복사 없이 위치만 지정:

```bash
export GOOGLE_CREDENTIALS=/경로/credentials.json   # 자격증명
export GOOGLE_TOKEN_PATH=/경로/token.pickle        # 토큰
# 영구 적용은 ~/.zshrc 에 위 줄 추가
```

## 참고
- `.google-cli` 는 점(`.`)으로 시작하는 숨김 폴더입니다. Finder에서 `Cmd+Shift+.` 로 숨김 파일을 표시할 수 있습니다.
- `credentials.json`, `token.pickle` 은 절대 커밋하지 마세요(자격증명).
