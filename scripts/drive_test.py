"""서비스 계정 Drive 쓰기 권한 테스트 — 루트에 임시 폴더 생성 후 삭제."""
import sys
from backend.core.config import get_settings, get_drive

settings = get_settings()
drive = get_drive()
if not drive:
    print("ERROR: get_drive() == None")
    sys.exit(1)

root = settings.drive_root_folder_id
print("쓰기 테스트: 루트에 '_perm_test_delete_me' 폴더 생성 시도...")
try:
    fid = drive.get_or_create_folder("_perm_test_delete_me", root)
    print(f"  → 생성 성공 (id={fid}) ✅  서비스 계정에 쓰기(편집자) 권한 있음")
except Exception as e:
    print(f"  → 생성 실패 ❌  {type(e).__name__}: {e}")
    print("  결론: 서비스 계정이 뷰어 권한 → 업로드 불가. 폴더 공유를 '편집자'로 올려야 함.")
    sys.exit(1)

print("정리: 임시 폴더 삭제 시도...")
try:
    drive.delete(fid)
    print("  → 삭제 성공 ✅  (생성·삭제 모두 가능 — 업로드/백업/삭제 전부 정상 작동)")
except Exception as e:
    print(f"  → 삭제 실패 ⚠  {type(e).__name__}: {e}")
    print("  (생성은 되나 삭제 불가 — 업로드는 되지만 Drive 정리/삭제는 소유권 문제로 막힐 수 있음)")
