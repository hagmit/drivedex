"""OAuth 2.0 authorization code flow with PKCE and a loopback redirect
(RFC 8252), usable for both Microsoft identity platform and Google."""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import http

DONE_PAGE = ("<!doctype html><meta charset=utf-8><title>drivedex</title>"
             "<p style='font-family:sans-serif'>サインインが完了しました。このタブは閉じてかまいません。</p>")


class AuthError(Exception):
    pass


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        if "code" not in params and "error" not in params:
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = params
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(DONE_PAGE.encode())

    def log_message(self, *args):
        pass


def stamp(token: dict, now=None) -> dict:
    now = time.time() if now is None else now
    token["expires_at"] = now + int(token.get("expires_in", 3600)) - 60
    return token


def authorize(auth_url, token_url, client_id, scopes, *, client_secret=None,
              redirect_host="localhost", extra_params=None, open_browser=webbrowser.open,
              timeout=300, echo=print) -> dict:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.result = None
    server.timeout = 1
    redirect_uri = f"http://{redirect_host}:{server.server_address[1]}"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    params = {
        "client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
        "scope": " ".join(scopes), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        **(extra_params or {}),
    }
    url = auth_url + "?" + urllib.parse.urlencode(params)
    echo("ブラウザでサインインしてください。開かない場合は次の URL を開きます:\n" + url)
    open_browser(url)
    deadline = time.time() + timeout
    try:
        while server.result is None and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    result = server.result
    if result is None:
        raise AuthError("サインインがタイムアウトしました")
    if result.get("state") != state:
        raise AuthError("state が一致しません（別のリクエストの応答）")
    if "error" in result:
        raise AuthError(f"{result['error']}: {result.get('error_description', '')}")
    data = {"grant_type": "authorization_code", "code": result["code"],
            "redirect_uri": redirect_uri, "client_id": client_id, "code_verifier": verifier}
    if client_secret:
        data["client_secret"] = client_secret
    return stamp(http.post_form(token_url, data))


def refresh(token_url, client_id, token: dict, *, scopes=None, client_secret=None) -> dict:
    if not token.get("refresh_token"):
        raise AuthError("refresh_token がありません。login し直してください")
    data = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"],
            "client_id": client_id}
    if scopes:
        data["scope"] = " ".join(scopes)
    if client_secret:
        data["client_secret"] = client_secret
    new = stamp(http.post_form(token_url, data))
    new.setdefault("refresh_token", token["refresh_token"])  # Google does not rotate
    return new
