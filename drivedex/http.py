"""Small HTTP helpers on top of urllib (standard library only)."""
from __future__ import annotations

import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "drivedex/0.1"
RETRY_STATUS = {429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} {url.split('?')[0]}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


_no_redirect_opener = urllib.request.build_opener(_NoRedirect)


def with_params(url: str, params: dict | None) -> str:
    if not params:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode(params)


def _open(method, url, headers, data, follow, timeout):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    opener = urllib.request.urlopen if follow else _no_redirect_opener.open
    return opener(req, timeout=timeout)


def request(method, url, *, headers=None, data=None, follow=True, retries=5,
            timeout=60, sleep=time.sleep):
    """Return (status, headers, body bytes). Retries 429/5xx honoring Retry-After."""
    for attempt in range(retries + 1):
        try:
            with _open(method, url, headers, data, follow, timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            if not follow and e.code in REDIRECT_STATUS:
                return e.code, dict(e.headers), body
            if e.code in RETRY_STATUS and attempt < retries:
                wait = e.headers.get("Retry-After")
                sleep(min(float(wait) if wait else 2 ** attempt, 60))
                continue
            raise HttpError(e.code, body.decode("utf-8", "replace"), url) from None
    raise AssertionError("unreachable")


def get_json(url, *, token=None, params=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    _, _, body = request("GET", with_params(url, params), headers=headers)
    return json.loads(body)


def post_form(url, data: dict):
    body = urllib.parse.urlencode(data).encode()
    _, _, out = request("POST", url, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    return json.loads(out)


def download(url, dest, *, token=None, timeout=300):
    """Stream url to dest. Redirects are followed WITHOUT the Authorization header
    so the bearer token never leaves the API host (pre-signed download URLs)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    for _ in range(5):
        try:
            r = _open("GET", url, headers, None, False, timeout)
        except urllib.error.HTTPError as e:
            if e.code in REDIRECT_STATUS:
                url, headers = e.headers["Location"], {}
                continue
            raise HttpError(e.code, e.read().decode("utf-8", "replace"), url) from None
        with r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, 1024 * 1024)
        return dest
    raise HttpError(310, "too many redirects", url)
