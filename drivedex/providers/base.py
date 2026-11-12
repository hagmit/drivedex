"""Provider interface shared by Microsoft Graph and Google Drive."""
from __future__ import annotations

import time
from dataclasses import dataclass

from .. import oauth
from .. import http


@dataclass
class Item:
    item_id: str
    parent_id: str | None
    name: str
    is_folder: bool
    size: int | None = None
    mtime: str | None = None
    modified_by: str | None = None
    web_url: str | None = None
    mime: str | None = None


@dataclass
class Deleted:
    item_id: str


@dataclass
class Cursor:
    value: str


@dataclass
class SourceSpec:
    label: str
    drive_id: str
    root_id: str | None = None


class NotConfigured(Exception):
    pass


class NotLoggedIn(Exception):
    pass


class ResyncRequired(Exception):
    """The provider discarded our change cursor; a full listing is needed."""


class OAuthProvider:
    """Common token handling. Subclasses set the endpoints and implement
    resolve / full / delta / download. full() and delta() are generators that
    yield Item / Deleted objects and finally exactly one Cursor."""

    name = ""
    redirect_host = "localhost"
    auth_extra: dict = {}

    def __init__(self, conf: dict, store, account: str | None = None):
        self.conf = conf
        self.store = store
        self.account = account or self.name
        if not conf.get("client_id"):
            raise NotConfigured(f"{self.name} の client_id が未設定です（drivedex config {self.name} ...）")

    # endpoints / scopes -------------------------------------------------
    @property
    def auth_url(self) -> str:
        raise NotImplementedError

    @property
    def token_url(self) -> str:
        raise NotImplementedError

    scopes: list[str] = []

    # tokens -------------------------------------------------------------
    def login(self, **kw) -> None:
        token = oauth.authorize(self.auth_url, self.token_url, self.conf["client_id"], self.scopes,
                                client_secret=self.conf.get("client_secret"),
                                redirect_host=self.redirect_host, extra_params=self.auth_extra, **kw)
        self.store.save(self.account, token)

    def access_token(self) -> str:
        token = self.store.load(self.account)
        if not token:
            raise NotLoggedIn(f"未ログインです（drivedex login {self.name}）")
        if token.get("expires_at", 0) <= time.time():
            token = oauth.refresh(self.token_url, self.conf["client_id"], token,
                                  scopes=self.scopes if self.name == "ms" else None,
                                  client_secret=self.conf.get("client_secret"))
            self.store.save(self.account, token)
        return token["access_token"]

    def get(self, url, params=None):
        return http.get_json(url, token=self.access_token(), params=params)

    # to implement ---------------------------------------------------------
    def resolve(self, **kw) -> list[SourceSpec]:
        raise NotImplementedError

    def full(self, spec: SourceSpec):
        raise NotImplementedError

    def delta(self, spec: SourceSpec, cursor: str):
        raise NotImplementedError

    def download(self, spec: SourceSpec, item: Item, dest_dir) -> str:
        raise NotImplementedError
