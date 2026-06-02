"""Drive 현황 전체 — 각 폴더의 파일 수/소유자, 에러도 표시."""
import sys, traceback
from backend.core.config import get_settings, get_drive

settings = get_settings()
drive = get_drive()
root = settings.drive_root_folder_id

def lst(fid):
    items, page = [], None
    while True:
        kw = dict(q=f"'{fid}' in parents and trashed=false",
                  fields="nextPageToken, files(id,name,mimeType,owners(emailAddress),ownedByMe)",
                  pageSize=1000)
        if page: kw["pageToken"] = page
        res = drive._call_api(lambda svc, k=kw: svc.files().list(**k).execute())
        items.extend(res.get("files", []))
        page = res.get("nextPageToken")
        if not page: break
    return items

try:
    months = lst(root)
    print(f"루트({root}) 하위 폴더 {len(months)}개")
    for m in months:
        docs = lst(m["id"]) if m["mimeType"].endswith("folder") else []
        print(f"\n[{m['name']}] — 문서 {len(docs)}개")
        for d in docs:
            files = lst(d["id"]) if d["mimeType"].endswith("folder") else []
            print(f"  - {d['name']}: {len(files)}개 항목")
            for c in files:
                owner = (c.get('owners') or [{}])[0].get('emailAddress','?')
                print(f"      {c['name']}  owner={owner} ownedByMe={c.get('ownedByMe')}")
    print("\n=== DONE ===")
except Exception:
    traceback.print_exc()
