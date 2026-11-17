"""Scheduled sync + notification (drivedex sync --notify).

Each run syncs every source, picks the changes logged since the previous run,
keeps only those under the configured folders, then
  - appends them to changes.log,
  - rewrites changes.html (the last 7 days, new ones marked),
  - shows a Windows toast that opens changes.html when clicked.
A source that fails (expired sign-in, network) does not stop the others; the
failure is notified once until it changes or clears.
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
import os
import subprocess
import time
import traceback
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import http, oauth
from .index import local_time, now_iso
from .providers import NotLoggedIn

STATE, LOCK, LOG, REPORT = "notify.json", "notify.lock", "changes.log", "changes.html"
REPORT_DAYS = 7
MARK = {"added": "+", "modified": "~", "deleted": "-"}
KIND = {"added": "追加", "modified": "更新", "deleted": "削除"}


# ------------------------------------------------------------ folder filter
def normalize_folder(folder: str) -> str:
    parts = [p for p in folder.replace("\\", "/").split("/") if p]
    return "/" + "/".join(parts)


def in_folders(path: str, folders: list[str]) -> bool:
    if not folders:
        return True
    for f in folders:
        if f == "/" or path == f or path.startswith(f + "/"):
            return True
    return False


# ------------------------------------------------------------------- lock
class Lock:
    """Keeps a manual run and a scheduled run from syncing at the same time."""

    def __init__(self, path: Path, stale: float = 2 * 3600):
        self.path, self.stale, self.held = path, stale, False

    def __enter__(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime < self.stale:
                        return False
                    self.path.unlink()  # left behind by a crashed run
                except FileNotFoundError:
                    pass
                continue
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
            return True
        return False

    def __exit__(self, *exc):
        if self.held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


# ------------------------------------------------------------------ toast
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
# The toast XML is passed in an environment variable, never pasted into the script:
# file names come from other people and must not become PowerShell code.
TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml($env:DRIVEDEX_TOAST)
$t = New-Object Windows.UI.Notifications.ToastNotification $x
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:DRIVEDEX_TOAST_APP).Show($t)
"""


def toast_xml(title: str, lines: list[str], launch: str | None = None) -> str:
    attrs = f" activationType=\"protocol\" launch={quoteattr(launch)}" if launch else ""
    texts = "".join(f"<text>{escape(t)}</text>" for t in [title, *lines][:3])
    return f"<toast{attrs}><visual><binding template=\"ToastGeneric\">{texts}</binding></visual></toast>"


def toast(title: str, lines: list[str], launch: str | None = None) -> bool:
    """Show a Windows toast through Windows PowerShell (no extra packages). False elsewhere or on failure."""
    if os.name != "nt":
        return False
    script = base64.b64encode(TOAST_SCRIPT.encode("utf-16-le")).decode()
    env = {**os.environ, "DRIVEDEX_TOAST": toast_xml(title, lines, launch), "DRIVEDEX_TOAST_APP": POWERSHELL_APP_ID}
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-EncodedCommand", script], env=env, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# ---------------------------------------------------------------- outputs
def describe_error(src, e: Exception) -> str:
    login = f"drivedex login {src.provider}" + (f" --account {src.account}" if src.account != src.provider else "")
    relogin = isinstance(e, (NotLoggedIn, oauth.AuthError)) or (
        isinstance(e, http.HttpError) and (e.status == 401 or "invalid_grant" in e.body))
    if relogin:
        return f"[{src.id}] {src.label}: サインインし直してください（{login}）"
    return f"[{src.id}] {src.label}: {type(e).__name__}: {str(e)[:200]}"


def append_log(path: Path, changes: list[dict], errors: list[str], at: str) -> None:
    lines = [f"{local_time(c['at'])} {MARK[c['kind']]} [{c['source_id']}]{c['path']}" for c in reversed(changes)]
    lines += [f"{local_time(at)} ! {e}" for e in errors]
    if lines:
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")


def render_report(changes: list[dict], new_keys: set, folders: list[str], errors: list[str], generated: str) -> str:
    rows = []
    for c in changes:
        name = html.escape(c["path"])
        if c.get("web_url") and c["kind"] != "deleted":
            name = f'<a href="{html.escape(c["web_url"])}">{name}</a>'
        new = ' class="new"' if (c["source_id"], c["item_id"], c["at"]) in new_keys else ""
        rows.append(f"<tr{new}><td>{local_time(c['at'])}</td><td class=\"k {c['kind']}\">{KIND[c['kind']]}</td>"
                    f"<td>{html.escape(c['label'])}</td><td>{name}</td>"
                    f"<td>{html.escape(c.get('modified_by') or '')}</td></tr>")
    scope = "、".join(html.escape(f) for f in folders) if folders else "すべてのフォルダ"
    errs = "".join(f"<p class=\"err\">{html.escape(e)}</p>" for e in errors)
    body = ("<table><thead><tr><th>日時</th><th>種類</th><th>ドライブ</th><th>パス</th><th>更新者</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>") if rows else "<p>変更はありません。</p>"
    return REPORT_TEMPLATE.format(generated=local_time(generated), days=REPORT_DAYS, scope=scope,
                                  errors=errs, body=body, count=len(new_keys))


REPORT_TEMPLATE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>drivedex 変更</title>
<style>
:root{{--bg:#fff;--fg:#1f2328;--mute:#656d76;--line:#d0d7de;--new:#fff8c5;--acc:#0969da;--add:#1a7f37;--del:#cf222e}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#0d1117;--fg:#e6edf3;--mute:#8d96a0;--line:#30363d;--new:#3b2e00;--acc:#4493f8;--add:#3fb950;--del:#f85149}}}}
:root[data-theme=dark]{{--bg:#0d1117;--fg:#e6edf3;--mute:#8d96a0;--line:#30363d;--new:#3b2e00;--acc:#4493f8;--add:#3fb950;--del:#f85149}}
*{{box-sizing:border-box}}body{{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Segoe UI","Yu Gothic UI",sans-serif}}
h1{{font-size:18px;margin:0 0 4px}}.mute{{color:var(--mute);font-size:12px}}a{{color:var(--acc)}}
.err{{color:var(--del);font-weight:600}}
.wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;margin-top:12px}}
th,td{{text-align:left;padding:4px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}
td:nth-child(4){{white-space:normal;word-break:break-all}}tr.new td{{background:var(--new)}}
.k.added{{color:var(--add)}}.k.deleted{{color:var(--del)}}
</style></head><body>
<h1>drivedex 変更（直近 {days} 日）</h1>
<div class="mute">{generated} 更新 ・ 対象: {scope} ・ 色付きは今回の新着 {count} 件</div>
{errors}<div class="wrap">{body}</div>
</body></html>
"""


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def folders(config) -> list[str]:
    return list(config.data.get("notify", {}).get("folders", []))


# -------------------------------------------------------------------- run
def run(app, *, notifier=toast) -> int:
    root = app.config.root
    with Lock(root / LOCK) as held:
        if not held:
            print("別の drivedex sync --notify が実行中です")
            return 0
        try:
            return _run(app, root, notifier)
        except Exception:  # a scheduled run has no console: leave the reason in the log
            append_log(root / LOG, [], ["予期しないエラー: " + traceback.format_exc().strip().replace("\n", " | ")],
                       now_iso())
            raise


def _run(app, root: Path, notifier) -> int:
    state = load_state(root / STATE)
    start = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    since = state.get("last") or (start - dt.timedelta(seconds=1)).isoformat()
    errors = []
    for src in app.index.sources():
        try:
            r = app.sync(src)
            print(f"[{src.id}] {src.label}: {r['mode']} 追加 {r['added']:,}・更新 {r['modified']:,}・削除 {r['deleted']:,}")
        except Exception as e:  # one broken source must not stop the others
            errors.append(describe_error(src, e))
    end = now_iso()
    scope = folders(app.config)
    new = [c for c in app.index.changes(since, after=True) if in_folders(c["path"], scope)]
    recent_since = (start - dt.timedelta(days=REPORT_DAYS)).isoformat()
    recent = [c for c in app.index.changes(recent_since) if in_folders(c["path"], scope)]
    keys = {(c["source_id"], c["item_id"], c["at"]) for c in new}
    report = root / REPORT
    report.write_text(render_report(recent, keys, scope, errors, end), "utf-8", newline="\n")
    append_log(root / LOG, new, errors, end)

    if new:
        lines = [f"{MARK[c['kind']]} {c['path'].lstrip('/')}" for c in new[:2]]
        if len(new) > 2:
            lines[-1] += f" ほか {len(new) - 2} 件"
        notifier(f"drivedex: {len(new)} 件の変更", lines, report.resolve().as_uri())
    error_text = "\n".join(errors)
    if errors and error_text != state.get("error"):  # same failure every hour is told only once
        notifier("drivedex: 取得できなかったドライブがあります", errors[:2], report.resolve().as_uri())
    state.update(last=end, error=error_text or None)
    (root / STATE).write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    print(f"新しい変更 {len(new)} 件 → {report}")
    for e in errors:
        print("取得できませんでした: " + e)
    return 1 if errors else 0
