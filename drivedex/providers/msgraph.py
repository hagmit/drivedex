"""SharePoint Online / OneDrive via Microsoft Graph (delegated, public client)."""
from __future__ import annotations

import os
import urllib.parse

from .. import http
from .base import Cursor, Deleted, Item, OAuthProvider, ResyncRequired, SourceSpec

GRAPH = "https://graph.microsoft.com/v1.0"
SELECT = "id,name,parentReference,size,lastModifiedDateTime,lastModifiedBy,webUrl,folder,file,deleted,root"


def parse_site_url(url: str) -> tuple[str, str]:
    """https://contoso.sharepoint.com/sites/team/Shared%20Documents/x -> (host, /sites/team)."""
    u = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    parts = [p for p in urllib.parse.unquote(u.path).split("/") if p]
    if len(parts) >= 2 and parts[0].lower() in ("sites", "teams", "personal"):
        return u.hostname, "/" + "/".join(parts[:2])
    return u.hostname, "/"


def to_item(d: dict):
    if "deleted" in d:
        return Deleted(d["id"])
    is_root = "root" in d
    by = ((d.get("lastModifiedBy") or {}).get("user") or {}).get("displayName")
    return Item(
        item_id=d["id"],
        parent_id=None if is_root else (d.get("parentReference") or {}).get("id"),
        name=d.get("name", ""),
        is_folder=is_root or "folder" in d,
        size=d.get("size"),
        mtime=d.get("lastModifiedDateTime"),
        modified_by=by,
        web_url=d.get("webUrl"),
        mime=(d.get("file") or {}).get("mimeType"),
    )


PERSONAL_TENANT = "consumers"


class GraphProvider(OAuthProvider):
    name = "ms"
    auth_extra = {"prompt": "select_account"}

    @property
    def tenant(self) -> str:
        return self.conf.get("tenant") or "organizations"

    @property
    def scopes(self) -> list[str]:
        # Sites.Read.All is not available to personal Microsoft accounts; asking for it fails sign-in.
        base = ["offline_access", "User.Read", "Files.Read.All"]
        return base if self.tenant == PERSONAL_TENANT else base + ["Sites.Read.All"]

    @property
    def auth_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/authorize"

    @property
    def token_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant}/oauth2/v2.0/token"

    def resolve(self, *, me=False, site=None, library=None, **_) -> list[SourceSpec]:
        if me:
            d = self.get(f"{GRAPH}/me/drive", {"$select": "id,name,owner"})
            owner = ((d.get("owner") or {}).get("user") or {}).get("displayName", "")
            return [SourceSpec(f"OneDrive {owner}".strip(), d["id"])]
        if not site:
            raise ValueError("--me か --site を指定してください")
        if self.tenant == PERSONAL_TENANT:
            raise ValueError("個人の Microsoft アカウント（--tenant consumers）では SharePoint サイトは使えません。--me を使います")
        host, path = parse_site_url(site)
        ref = f"{GRAPH}/sites/{host}" + (f":{urllib.parse.quote(path)}" if path != "/" else "")
        s = self.get(ref, {"$select": "id,displayName"})
        drives = self.get(f"{GRAPH}/sites/{s['id']}/drives", {"$select": "id,name,webUrl,driveType"})["value"]
        out = []
        for d in drives:
            if d.get("driveType") != "documentLibrary":
                continue
            tail = urllib.parse.unquote(d.get("webUrl", "")).rstrip("/").split("/")[-1]
            if library and library not in (d["name"], tail):
                continue
            out.append(SourceSpec(f"{s.get('displayName', host)} / {d['name']}", d["id"]))
        if not out:
            raise ValueError("該当するドキュメントライブラリがありません")
        return out

    def _walk(self, url, params=None):
        while url:
            try:
                page = self.get(url, params)
            except http.HttpError as e:
                if e.status == 410:
                    raise ResyncRequired() from None
                raise
            params = None  # nextLink / deltaLink already carry the query
            for d in page.get("value", []):
                yield to_item(d)
            url = page.get("@odata.nextLink")
            if not url:
                yield Cursor(page["@odata.deltaLink"])

    def full(self, spec: SourceSpec):
        yield from self._walk(f"{GRAPH}/drives/{spec.drive_id}/root/delta", {"$select": SELECT})

    def delta(self, spec: SourceSpec, cursor: str):
        yield from self._walk(cursor)

    def download(self, spec: SourceSpec, item: Item, dest_dir) -> str:
        dest = os.path.join(dest_dir, item.name)
        http.download(f"{GRAPH}/drives/{spec.drive_id}/items/{item.item_id}/content", dest,
                      token=self.access_token())
        return dest
