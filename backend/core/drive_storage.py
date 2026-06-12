"""Google Drive — S3-like abstraction layer for samples/ and extracted/.

인증: Google CLI OAuth (credentials.json + token.json)
Transport: requests (urllib3) — httplib2 완전 제거 (httplib2 C SSL이 대용량 업로드 시
  heap corruption → SIGABRT/SEGV를 일으켜 프로세스 전체를 죽이는 버그 회피)
"""
import logging
import ssl
import time
from pathlib import Path
from typing import Optional

logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_BASE = "https://www.googleapis.com/drive/v3"
_UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3"


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
        self._archive_folder_id = archive_folder_id
        self._inbox_folder_id = inbox_folder_id
        self._rs = None
        self._folder_cache: dict[tuple[str, str], str] = {}

    # ── auth / session ────────────────────────────────────────────────────────

    def _auth(self):
        import pickle
        from google.auth.transport.requests import Request

        with open(self._token_path, "rb") as f:
            creds = pickle.load(f)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return creds

    @property
    def _session(self):
        if self._rs is None:
            from google.auth.transport.requests import AuthorizedSession
            self._rs = AuthorizedSession(self._auth())
        return self._rs

    # ── internal helpers ──────────────────────────────────────────────────────

    def _get(self, path: str, **kwargs) -> dict:
        return self._req("get", path, **kwargs)

    def _post(self, path: str, **kwargs) -> dict:
        return self._req("post", path, **kwargs)

    def _patch(self, path: str, **kwargs) -> dict:
        return self._req("patch", path, **kwargs)

    def _delete_req(self, path: str, **kwargs) -> None:
        self._req("delete", path, expect_json=False, **kwargs)

    def _req(self, method: str, path: str, expect_json: bool = True, **kwargs) -> dict:
        url = path if path.startswith("http") else f"{_BASE}/{path.lstrip('/')}"
        for attempt in range(3):
            try:
                resp = getattr(self._session, method)(url, **kwargs)
                resp.raise_for_status()
                return resp.json() if expect_json and resp.content else {}
            except (ssl.SSLError, OSError) as exc:
                if attempt == 2:
                    raise
                logging.getLogger(__name__).warning(
                    "Drive API SSL/OS error (attempt %d, %s %s): %s — retrying",
                    attempt + 1, method.upper(), path, exc,
                )
                self._rs = None
                time.sleep(2 ** attempt)
        raise RuntimeError("_req: unreachable")  # pragma: no cover

    # ── folder helpers ────────────────────────────────────────────────────────

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        key = (name, parent_id)
        if key in self._folder_cache:
            return self._folder_cache[key]
        existing = self._find(name, parent_id, is_folder=True)
        if existing:
            self._folder_cache[key] = existing
            return existing
        res = self._post("files", json={
            "name": name,
            "mimeType": _FOLDER_MIME,
            "parents": [parent_id],
        }, params={"fields": "id"})
        fid = res["id"]
        self._folder_cache[key] = fid
        return fid

    def find_folder(self, name: str, parent_id: str) -> Optional[str]:
        return self._find(name, parent_id, is_folder=True)

    def find_file(self, name: str, parent_id: str) -> Optional[str]:
        return self._find(name, parent_id, is_folder=False)

    def _find(self, name: str, parent_id: str, *, is_folder: bool) -> Optional[str]:
        op = "=" if is_folder else "!="
        q = (
            f"'{parent_id}' in parents"
            f" and mimeType {op} '{_FOLDER_MIME}'"
            f" and trashed = false"
        )
        items: list[dict] = []
        page_token = None
        while True:
            params: dict = {"q": q, "fields": "nextPageToken,files(id,name)", "pageSize": 1000}
            if page_token:
                params["pageToken"] = page_token
            res = self._get("files", params=params)
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
        """Upload/overwrite file via resumable upload. Returns file_id."""
        name = name or local_path.name
        existing = self.find_file(name, parent_id)
        file_size = local_path.stat().st_size

        for attempt in range(3):
            try:
                return self._resumable_upload(local_path, parent_id, name, existing, file_size)
            except (ssl.SSLError, OSError) as exc:
                if attempt == 2:
                    raise
                logging.getLogger(__name__).warning(
                    "upload_file error (attempt %d, %s): %s — retrying", attempt + 1, name, exc
                )
                self._rs = None
                time.sleep(2 ** attempt)
        raise RuntimeError("upload_file: unreachable")  # pragma: no cover

    def _resumable_upload(
        self,
        local_path: Path,
        parent_id: str,
        name: str,
        existing_id: Optional[str],
        file_size: int,
    ) -> str:
        session = self._session
        init_headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(file_size),
        }
        if existing_id:
            init_resp = session.patch(
                f"{_UPLOAD_BASE}/files/{existing_id}",
                params={"uploadType": "resumable"},
                headers=init_headers,
                json={},
            )
        else:
            init_resp = session.post(
                f"{_UPLOAD_BASE}/files",
                params={"uploadType": "resumable"},
                headers=init_headers,
                json={"name": name, "parents": [parent_id]},
            )
        init_resp.raise_for_status()
        upload_url = init_resp.headers["Location"]

        with open(local_path, "rb") as fh:
            upload_resp = session.put(
                upload_url,
                data=fh,
                headers={
                    "Content-Length": str(file_size),
                    "Content-Type": "application/octet-stream",
                },
            )
        upload_resp.raise_for_status()
        return upload_resp.json().get("id") or existing_id

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
                resp = self._session.get(
                    f"{_BASE}/files/{file_id}",
                    params={"alt": "media"},
                    stream=True,
                )
                resp.raise_for_status()
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        fh.write(chunk)
                return
            except (ssl.SSLError, OSError) as exc:
                if attempt == 2:
                    raise
                self._rs = None
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
            params: dict = {
                "q": (
                    f"'{folder_id}' in parents"
                    " and mimeType='application/pdf'"
                    " and trashed = false"
                ),
                "fields": "nextPageToken,files(id,name,createdTime)",
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            res = self._get("files", params=params)
            items.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        return items

    def move_file(self, file_id: str, new_parent_id: str, old_parent_id: str = "") -> None:
        params: dict = {"addParents": new_parent_id, "fields": "id"}
        if old_parent_id:
            params["removeParents"] = old_parent_id
        self._patch(f"files/{file_id}", params=params)

    # ── list / delete ─────────────────────────────────────────────────────────

    def list_folder(self, folder_id: str) -> list[dict]:
        """Returns [{id, name, mimeType}]."""
        items: list[dict] = []
        page_token = None
        while True:
            params: dict = {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": "nextPageToken,files(id,name,mimeType)",
                "pageSize": 1000,
            }
            if page_token:
                params["pageToken"] = page_token
            res = self._get("files", params=params)
            items.extend(res.get("files", []))
            page_token = res.get("nextPageToken")
            if not page_token:
                break
        return items

    def delete(self, file_id: str) -> None:
        self._delete_req(f"files/{file_id}")

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
        base_fid = self._archive_folder_id or root_folder_id
        month_fid = self.get_or_create_folder(hatsu_month, base_fid)
        return self.get_or_create_folder(doc_id, month_fid)

    def push_pdf(self, root_folder_id: str, hatsu_month: str, pdf_path: Path, doc_id: str) -> None:
        if not hatsu_month or not pdf_path.exists():
            return
        doc_fid = self._get_doc_folder(root_folder_id, hatsu_month, doc_id)
        self.upload_file(pdf_path, doc_fid)

    def push_pages(self, root_folder_id: str, hatsu_month: str, pages_dir: Path, doc_id: str) -> None:
        if not hatsu_month or not pages_dir.exists():
            return
        doc_fid = self._get_doc_folder(root_folder_id, hatsu_month, doc_id)
        ocr_fid = self.get_or_create_folder("ocr", doc_fid)
        for f in sorted(pages_dir.iterdir()):
            if f.is_file():
                self.upload_file(f, ocr_fid)

    def push_extracted(self, root_folder_id: str, hatsu_month: str, extracted_dir: Path, doc_id: str) -> None:
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
        """Upload doc files to Drive archive."""
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
        for base_fid in filter(None, [self._archive_folder_id, root_folder_id]):
            month_fid = self.find_folder(hatsu_month, base_fid)
            if not month_fid:
                continue
            doc_fid = self.find_folder(doc_id, month_fid)
            if doc_fid:
                return doc_fid
        return None

    def pull_pdf(self, root_folder_id: str, hatsu_month: str, doc_id: str, pdf_filename: str, dest_dir: Path) -> bool:
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
        if hatsu_month:
            doc_fid = self._find_doc_folder(root_folder_id, hatsu_month, doc_id)
            if doc_fid:
                self.delete(doc_fid)
                return

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
