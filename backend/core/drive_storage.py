"""Google Drive — S3-like abstraction layer for samples/ and extracted/.

인증: Google CLI OAuth (credentials.json + token.json)
"""
import http.client
import logging
import ssl
import time
from pathlib import Path
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

# drive.file은 "이 자격증명이 직접 만든 파일"만 접근 가능 → 다른 주체(예: 과거 OAuth)가
# 올린 파일은 못 읽음. 서비스 계정으로 공유 폴더의 기존 파일을 읽고 쓰려면 전체 drive 스코프 필요.
SCOPES = ["https://www.googleapis.com/auth/drive"]
_FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveStorage:
    """Read/write files on Google Drive.

    Pipeline files stay local during processing; call push_doc() after completion
    to upload and clean up locals. Call pull_*() to restore before re-processing.
    """

    def __init__(
        self,
        credentials_path: Path,
        token_path: Path,
        archive_folder_id: str = "",
        inbox_folder_id: str = "",
    ):
        self._credentials_path = credentials_path
        self._token_path = token_path
        self._archive_folder_id = archive_folder_id  # rebate-archive/ folder
        self._inbox_folder_id = inbox_folder_id      # rebate-inbox/ folder
        self._svc = None
        self._folder_cache: dict[tuple[str, str], str] = {}  # (name, parent_id) -> folder_id

    @property
    def _service(self):
        if self._svc is None:
            self._svc = build("drive", "v3", credentials=self._auth())
        return self._svc

    def _auth(self):
        import pickle
        from google.auth.transport.requests import Request

        with open(self._token_path, "rb") as f:
            creds = pickle.load(f)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds

    def _call_api(self, fn):
        """Drive API 호출 래퍼 — SSLError/IncompleteRead 발생 시 서비스 재생성 후 최대 3회 재시도."""
        for attempt in range(3):
            try:
                return fn(self._service)
            except (ssl.SSLError, http.client.IncompleteRead):
                if attempt == 2:
                    raise
                self._svc = None
                time.sleep(2 ** attempt)

    # ── folder helpers ────────────────────────────────────────────────────────

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        key = (name, parent_id)
        if key in self._folder_cache:
            return self._folder_cache[key]
        existing = self._find(name, parent_id, is_folder=True)
        if existing:
            self._folder_cache[key] = existing
            return existing
        meta = {"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]}
        fid = self._call_api(
            lambda svc: svc.files().create(body=meta, fields="id").execute()
        )["id"]
        self._folder_cache[key] = fid
        return fid

    def find_folder(self, name: str, parent_id: str) -> Optional[str]:
        return self._find(name, parent_id, is_folder=True)

    def find_file(self, name: str, parent_id: str) -> Optional[str]:
        return self._find(name, parent_id, is_folder=False)

    def _find(self, name: str, parent_id: str, *, is_folder: bool) -> Optional[str]:
        # Drive search index can lag after creation; omit name from query and filter in Python
        op = "=" if is_folder else "!="
        q = (
            f"'{parent_id}' in parents"
            f" and mimeType {op} '{_FOLDER_MIME}'"
            f" and trashed = false"
        )
        items: list[dict] = []
        page_token = None
        while True:
            kwargs: dict = dict(q=q, fields="nextPageToken, files(id,name)", pageSize=1000)
            if page_token:
                kwargs["pageToken"] = page_token
            res = self._call_api(lambda svc, kw=kwargs: svc.files().list(**kw).execute())
            items.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        for f in items:
            if f["name"] == name:
                return f["id"]
        return None

    # ── upload ────────────────────────────────────────────────────────────────

    def upload_file(self, local_path: Path, parent_id: str, name: Optional[str] = None) -> str:
        """Upload/overwrite file. Returns file_id."""
        name = name or local_path.name
        existing = self.find_file(name, parent_id)
        path_str = str(local_path)
        if existing:
            f = self._call_api(
                lambda svc, eid=existing: svc.files().update(
                    fileId=eid,
                    media_body=MediaFileUpload(path_str, resumable=True),
                    fields="id",
                ).execute()
            )
        else:
            meta = {"name": name, "parents": [parent_id]}
            f = self._call_api(
                lambda svc, m=meta: svc.files().create(
                    body=m,
                    media_body=MediaFileUpload(path_str, resumable=True),
                    fields="id",
                ).execute()
            )
        return f["id"]

    def upload_dir(self, local_dir: Path, parent_id: str) -> str:
        """Upload directory tree recursively. Returns folder_id."""
        folder_id = self.get_or_create_folder(local_dir.name, parent_id)
        for item in sorted(local_dir.iterdir()):
            if item.is_file():
                self.upload_file(item, folder_id)
            elif item.is_dir():
                self.upload_dir(item, folder_id)
        return folder_id

    # ── download ──────────────────────────────────────────────────────────────

    def download_file(self, file_id: str, dest_path: Path) -> None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            try:
                request = self._call_api(lambda svc: svc.files().get_media(fileId=file_id))
                with open(dest_path, "wb") as fh:
                    dl = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done:
                        _, done = dl.next_chunk()
                return
            except (ssl.SSLError, http.client.IncompleteRead):
                if attempt == 2:
                    raise
                self._svc = None
                time.sleep(2 ** attempt)

    def download_dir(self, folder_id: str, dest_path: Path) -> None:
        """Download folder tree recursively to dest_path."""
        dest_path.mkdir(parents=True, exist_ok=True)
        for item in self.list_folder(folder_id):
            local = dest_path / item["name"]
            if item["mimeType"] == _FOLDER_MIME:
                self.download_dir(item["id"], local)
            else:
                self.download_file(item["id"], local)

    def list_pdf_files(self, folder_id: str) -> list[dict]:
        """List PDF files in folder. Returns [{id, name, createdTime}]."""
        items: list[dict] = []
        page_token = None
        while True:
            kwargs: dict = dict(
                q=(
                    f"'{folder_id}' in parents"
                    " and mimeType='application/pdf'"
                    " and trashed = false"
                ),
                fields="nextPageToken, files(id,name,createdTime)",
                pageSize=100,
            )
            if page_token:
                kwargs["pageToken"] = page_token
            res = self._call_api(lambda svc, kw=kwargs: svc.files().list(**kw).execute())
            items.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        return items

    def move_file(self, file_id: str, new_parent_id: str, old_parent_id: str = "") -> None:
        """Move file to new_parent_id (Drive update parents)."""
        kwargs: dict = {"fileId": file_id, "addParents": new_parent_id, "fields": "id"}
        if old_parent_id:
            kwargs["removeParents"] = old_parent_id
        self._call_api(lambda svc, kw=kwargs: svc.files().update(**kw).execute())

    # ── list / delete ─────────────────────────────────────────────────────────

    def list_folder(self, folder_id: str) -> list[dict]:
        """Returns [{id, name, mimeType}]."""
        items: list[dict] = []
        page_token = None
        while True:
            kwargs: dict = dict(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id,name,mimeType)",
                pageSize=1000,
            )
            if page_token:
                kwargs["pageToken"] = page_token
            res = self._call_api(lambda svc, kw=kwargs: svc.files().list(**kw).execute())
            items.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        return items

    def delete(self, file_id: str) -> None:
        self._call_api(lambda svc: svc.files().delete(fileId=file_id).execute())

    def delete_by_name(self, name: str, parent_id: str) -> bool:
        """Delete file or folder by name. Returns True if found and deleted."""
        fid = self._find(name, parent_id, is_folder=False) or self._find(
            name, parent_id, is_folder=True
        )
        if fid:
            self.delete(fid)
            return True
        return False

    # ── high-level doc operations ─────────────────────────────────────────────

    def _get_doc_folder(self, root_folder_id: str, hatsu_month: str, doc_id: str) -> str:
        """archive 우선, root fallback 으로 {hatsu_month}/{doc_id} 폴더를 가져오거나 생성."""
        base_fid = self._archive_folder_id or root_folder_id
        month_fid = self.get_or_create_folder(hatsu_month, base_fid)
        return self.get_or_create_folder(doc_id, month_fid)

    def push_pdf(self, root_folder_id: str, hatsu_month: str, pdf_path: Path, doc_id: str) -> None:
        """업로드 직후 — 원본 PDF만 Drive에 올린다."""
        if not hatsu_month or not pdf_path.exists():
            return
        doc_fid = self._get_doc_folder(root_folder_id, hatsu_month, doc_id)
        self.upload_file(pdf_path, doc_fid)

    def push_pages(self, root_folder_id: str, hatsu_month: str, pages_dir: Path, doc_id: str) -> None:
        """OCR 완료 후 — 페이지 이미지·OCR 파일만 Drive에 올린다."""
        if not hatsu_month or not pages_dir.exists():
            return
        doc_fid = self._get_doc_folder(root_folder_id, hatsu_month, doc_id)
        ocr_fid = self.get_or_create_folder("ocr", doc_fid)
        for f in sorted(pages_dir.iterdir()):
            if f.is_file():
                self.upload_file(f, ocr_fid)

    def push_extracted(self, root_folder_id: str, hatsu_month: str, extracted_dir: Path, doc_id: str) -> None:
        """Phase 4 완료 후 — extracted/ 결과물만 Drive에 올린다."""
        if not hatsu_month:
            return
        doc_extracted = extracted_dir / doc_id
        if not doc_extracted.exists():
            return
        doc_fid = self._get_doc_folder(root_folder_id, hatsu_month, doc_id)
        ext_fid = self.get_or_create_folder("extracted", doc_fid)
        for f in sorted(doc_extracted.iterdir()):
            if f.is_file():
                self.upload_file(f, ext_fid)

    def push_doc(
        self,
        root_folder_id: str,
        hatsu_month: str,
        pdf_filename: str,
        samples_dir: Path,
        extracted_dir: Path,
        doc_id: str,
    ) -> None:
        """Upload doc files to Drive archive.

        Drive layout (archive_folder_id 설정 시):
          archive/{hatsu_month}/{doc_id}/{pdf_filename}
          archive/{hatsu_month}/{doc_id}/ocr/
          archive/{hatsu_month}/{doc_id}/extracted/
        미설정 시: root/{hatsu_month}/{doc_id}/...
        """
        if not hatsu_month:
            logging.getLogger(__name__).warning("[%s] hatsu_month 없음 — Drive 업로드 건너뜀", doc_id)
            return
        base_fid = self._archive_folder_id or root_folder_id
        month_fid = self.get_or_create_folder(hatsu_month, base_fid)
        doc_fid = self.get_or_create_folder(doc_id, month_fid)

        pdf_path = samples_dir / pdf_filename
        pages_dir = samples_dir / f"{doc_id}_pages"
        doc_extracted = extracted_dir / doc_id

        if pdf_path.exists():
            self.upload_file(pdf_path, doc_fid)
        if pages_dir.exists():
            ocr_fid = self.get_or_create_folder("ocr", doc_fid)
            for f in sorted(pages_dir.iterdir()):
                if f.is_file():
                    self.upload_file(f, ocr_fid)
        if doc_extracted.exists():
            ext_fid = self.get_or_create_folder("extracted", doc_fid)
            for f in sorted(doc_extracted.iterdir()):
                if f.is_file():
                    self.upload_file(f, ext_fid)

    def _find_doc_folder(self, root_folder_id: str, hatsu_month: str, doc_id: str) -> Optional[str]:
        """archive → root 순으로 {hatsu_month}/{doc_id} 폴더 ID 탐색."""
        for base_fid in filter(None, [self._archive_folder_id, root_folder_id]):
            month_fid = self.find_folder(hatsu_month, base_fid)
            if not month_fid:
                continue
            doc_fid = self.find_folder(doc_id, month_fid)
            if doc_fid:
                return doc_fid
        return None

    def pull_pdf(self, root_folder_id: str, hatsu_month: str, doc_id: str, pdf_filename: str, dest_dir: Path) -> bool:
        """Download PDF from Drive (archive-first) to dest_dir. Returns True if found."""
        if not hatsu_month:
            return False
        doc_fid = self._find_doc_folder(root_folder_id, hatsu_month, doc_id)
        if not doc_fid:
            return False
        file_id = self.find_file(pdf_filename, doc_fid)
        if not file_id:
            return False
        self.download_file(file_id, dest_dir / pdf_filename)
        return True

    def pull_pages(self, root_folder_id: str, hatsu_month: str, doc_id: str, dest_samples_dir: Path) -> bool:
        """Download ocr/ contents from Drive to {doc_id}_pages/. Returns True if found."""
        if not hatsu_month:
            return False
        doc_fid = self._find_doc_folder(root_folder_id, hatsu_month, doc_id)
        if not doc_fid:
            return False
        ocr_fid = self.find_folder("ocr", doc_fid)
        if not ocr_fid:
            return False
        self.download_dir(ocr_fid, dest_samples_dir / f"{doc_id}_pages")
        return True

    def pull_extracted(self, root_folder_id: str, hatsu_month: str, doc_id: str, dest_extracted_dir: Path) -> bool:
        """Download extracted/ contents from Drive. Returns True if found."""
        if not hatsu_month:
            return False
        doc_fid = self._find_doc_folder(root_folder_id, hatsu_month, doc_id)
        if not doc_fid:
            return False
        ext_fid = self.find_folder("extracted", doc_fid)
        if not ext_fid:
            return False
        self.download_dir(ext_fid, dest_extracted_dir / doc_id)
        return True

    def delete_doc(self, root_folder_id: str, hatsu_month: str, doc_id: str,
                   pdf_filename: str = "") -> None:
        """Delete all Drive files for a document.
        Archive structure (archive-first): archive/{hatsu_month}/{doc_id}/
        Root structure (fallback):         root/{hatsu_month}/{doc_id}/
        Old structure (fallback):          root/samples/... + root/extracted/...
        """
        # New structure — archive 우선, root fallback (_find_doc_folder 사용)
        if hatsu_month:
            doc_fid = self._find_doc_folder(root_folder_id, hatsu_month, doc_id)
            if doc_fid:
                self.delete(doc_fid)
                return

        # Old structure fallback
        if not root_folder_id:
            return
        samples_fid = self.find_folder("samples", root_folder_id)
        if samples_fid:
            if pdf_filename:
                self.delete_by_name(pdf_filename, samples_fid)
            self.delete_by_name(f"{doc_id}_pages", samples_fid)
        extracted_fid = self.find_folder("extracted", root_folder_id)
        if extracted_fid:
            self.delete_by_name(doc_id, extracted_fid)
