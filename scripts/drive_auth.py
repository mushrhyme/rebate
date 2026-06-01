#!/usr/bin/env python3
"""Google Drive 최초 인증 — 한 번만 실행하면 token.json이 저장됩니다.

실행 방법:
    cd /Users/nongshim/Desktop/Python/lesson/Lecture
    python scripts/drive_auth.py

브라우저가 열리면 Google 계정으로 로그인 후 권한을 허용하세요.
token.json이 생성되면 백엔드 서버는 자동으로 토큰을 갱신합니다.

.env에 DRIVE_ROOT_FOLDER_ID를 설정해야 Drive가 활성화됩니다:
    DRIVE_ROOT_FOLDER_ID=<Drive 폴더 ID>

Drive 폴더 ID는 브라우저에서 폴더를 열었을 때 URL 끝 부분입니다:
    https://drive.google.com/drive/folders/<이 부분이 folder_id>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from backend.core.config import get_settings
from backend.core.drive_storage import DriveStorage


def main() -> None:
    settings = get_settings()
    print(f"credentials.json: {settings.drive_credentials_path}")
    print(f"token.json 저장 위치: {settings.drive_token_path}")

    sa_path = settings.drive_service_account_path
    if sa_path.exists():
        print(f"서비스 계정 인증 사용: {sa_path}")
        drive = DriveStorage(settings.drive_credentials_path, settings.drive_token_path, service_account_path=sa_path)
    else:
        print("OAuth 인증 사용 (service_account.json 없음)")
        if not settings.drive_credentials_path.exists():
            print("\n오류: credentials.json 없음 — Google Cloud Console에서 다운로드 후 프로젝트 루트에 넣으세요.")
            sys.exit(1)
        drive = DriveStorage(settings.drive_credentials_path, settings.drive_token_path)

    # 서비스 접근으로 인증 트리거
    _ = drive._service
    print(f"\n인증 완료! token.json 저장됨: {settings.drive_token_path}")

    if not settings.drive_root_folder_id:
        print("\n다음 단계: backend/.env에 아래를 추가하세요.")
        print("  DRIVE_ROOT_FOLDER_ID=<Drive 폴더 ID>")
        return

    print(f"\nDrive 루트 폴더 ID: {settings.drive_root_folder_id}")
    items = drive.list_folder(settings.drive_root_folder_id)
    print(f"폴더 내 항목 수: {len(items)}")
    for item in items[:10]:
        print(f"  - {item['name']} ({item['mimeType'].split('.')[-1]})")


if __name__ == "__main__":
    main()
