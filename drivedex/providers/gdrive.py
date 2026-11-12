"""Google Drive (My Drive and shared drives) via Drive API v3."""
from __future__ import annotations

import os

from .. import http
from .base import Cursor, Deleted, Item, OAuthProvider, ResyncRequired, SourceSpec

API = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,mimeType,parents,size,modifiedTime,lastModifyingUser/displayName,webViewLink,trashed"
MY_DRIVE = "mydrive"
ORPHAN = "~orphan"  # shared-with-me items have no parent in our tree
# Google-native documents cannot be downloaded as-is; export them to Office formats.
EXPORT = {
    "application/vnd.google-apps.document":
        ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet":
        ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation":
        ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}


def to_item(f: dict):
    if f.get("trashed"):
        return Deleted(f["id"])
    parents = f.get("parents") or [ORPHAN]
    return Item(
        item_id=f["id"],
        parent_id=parents[0],
        name=f.get("name", ""),
        is_folder=f.get("mimeType") == FOLDER,
        size=int(f["size"]) if f.get("size") is not None else None,
        mtime=f.get("modifiedTime"),
        modified_by=(f.get("lastModifyingUser") or {}).get("displayName"),
        web_url=f.get("webViewLink"),
        mime=f.get("mimeType"),
    )


class DriveProvider(OAuthProvider):
    name = "google"
    redirect_host = "127.0.0.1"
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
    auth_extra = {"access_type": "offline", "prompt": "consent"}

    def _scope(self, spec: SourceSpec) -> dict:
        if spec.drive_id == MY_DRIVE:
            return {"corpora": "user"}
        return {"corpora": "drive", "driveId": spec.drive_id,
                "includeItemsFromAllDrives": "true", "supportsAllDrives": "true"}

    def resolve(self, *, my_drive=False, shared_drive=None, **_) -> list[SourceSpec]:
        if my_drive:
            root = self.get(f"{API}/files/root", {"fields": "id"})
            return [SourceSpec("Google マイドライブ", MY_DRIVE, root["id"])]
        out, token = [], None
        while True:
            page = self.get(f"{API}/drives", {"pageSize": 100, **({"pageToken": token} if token else {})})
            for d in page.get("drives", []):
                if shared_drive in (None, "", d["name"]):
                    out.append(SourceSpec(f"共有ドライブ {d['name']}", d["id"], d["id"]))
            token = page.get("nextPageToken")
            if not token:
                break
        if not out:
            raise ValueError("該当する共有ドライブがありません")
        return out

    def full(self, spec: SourceSpec):
        sc = self._scope(spec)
        cparams = {k: v for k, v in sc.items() if k in ("driveId", "supportsAllDrives")}
        start = self.get(f"{API}/changes/startPageToken", cparams)["startPageToken"]
        root_name = "マイドライブ" if spec.drive_id == MY_DRIVE else spec.label
        yield Item(spec.root_id, None, root_name, True)
        token = None
        while True:
            params = {**sc, "q": "trashed=false", "pageSize": 1000,
                      "fields": f"nextPageToken,files({FILE_FIELDS})"}
            if token:
                params["pageToken"] = token
            page = self.get(f"{API}/files", params)
            for f in page.get("files", []):
                yield to_item(f)
            token = page.get("nextPageToken")
            if not token:
                break
        yield Cursor(start)

    def delta(self, spec: SourceSpec, cursor: str):
        sc = self._scope(spec)
        extra = {k: v for k, v in sc.items() if k != "corpora"}
        token = cursor
        while True:
            params = {**extra, "pageToken": token, "pageSize": 1000,
                      "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,file({FILE_FIELDS}))"}
            try:
                page = self.get(f"{API}/changes", params)
            except http.HttpError as e:
                if e.status in (400, 404, 410) and "pageToken" in e.body:
                    raise ResyncRequired() from None
                raise
            for c in page.get("changes", []):
                if c.get("removed") or not c.get("file"):
                    yield Deleted(c["fileId"])
                else:
                    yield to_item(c["file"])
            if page.get("newStartPageToken"):
                yield Cursor(page["newStartPageToken"])
                return
            token = page["nextPageToken"]

    def download(self, spec: SourceSpec, item: Item, dest_dir) -> str:
        if item.mime in EXPORT:
            mime, ext = EXPORT[item.mime]
            url = http.with_params(f"{API}/files/{item.item_id}/export", {"mimeType": mime})
            dest = os.path.join(dest_dir, item.name + ext)
        else:
            url = http.with_params(f"{API}/files/{item.item_id}", {"alt": "media", "supportsAllDrives": "true"})
            dest = os.path.join(dest_dir, item.name)
        http.download(url, dest, token=self.access_token())
        return dest
