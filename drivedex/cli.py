"""drivedex command line."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import webbrowser
from pathlib import Path

from . import __version__, officemd, providers
from .config import Config
from .index import Index
from .providers import NotConfigured, NotLoggedIn, ResyncRequired
from .secure_store import TokenStore
from .viewer import export


class App:
    def __init__(self, root: Path | None = None):
        self.config = Config(root)
        self.store = TokenStore(self.config.root)
        self.config.root.mkdir(parents=True, exist_ok=True)
        self.index = Index(self.config.root / "index.db")

    def provider(self, name, account=None):
        return providers.get(name, self.config, self.store, account)

    def sync(self, src, *, full=False, provider=None) -> dict:
        p = provider or self.provider(src.provider, src.account)
        if src.cursor and not full:
            try:
                return {"mode": "delta", **self.index.apply(src.id, p.delta(src.spec, src.cursor), full=False)}
            except ResyncRequired:
                pass  # cursor expired on the provider side: fall back to a full listing
        return {"mode": "full", **self.index.apply(src.id, p.full(src.spec), full=True)}


def _since(days: float) -> str:
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    return t.replace(microsecond=0).isoformat()


def cmd_config(app, a):
    if a.provider == "google" and a.client_secret_json:
        raw = json.loads(Path(a.client_secret_json).read_text("utf-8"))
        inst = raw.get("installed") or raw.get("web") or raw
        a.client_id, a.client_secret = inst["client_id"], inst.get("client_secret")
    app.config.set_provider(a.provider, {"client_id": a.client_id, "client_secret": a.client_secret,
                                         "tenant": getattr(a, "tenant", None)})
    print(f"{a.provider} の設定を保存しました: {app.config.path}")


def cmd_login(app, a):
    app.provider(a.provider, a.account).login()
    print("ログインしました（トークンは暗号化して保存）")


def cmd_logout(app, a):
    print("ログアウトしました" if app.store.delete(a.account or a.provider) else "保存済みのトークンはありません")


def cmd_add(app, a):
    p = app.provider(a.provider, a.account)
    specs = p.resolve(me=a.me, site=a.site, library=a.library, my_drive=a.my_drive, shared_drive=a.shared_drive)
    for s in specs:
        sid = app.index.add_source(a.provider, p.account, s)
        print(f"[{sid}] {s.label}")
    print("drivedex sync で取得します")


def cmd_sources(app, a):
    for s in app.index.sources():
        n = app.index.db.execute("SELECT COUNT(*) FROM items WHERE source_id=?", (s.id,)).fetchone()[0]
        print(f"[{s.id}] {s.provider:6} {s.label}  {n:,} 件  最終同期 {s.synced_at or '未'}")


def cmd_remove(app, a):
    app.index.remove_source(a.id)
    print(f"[{a.id}] を索引から外しました")


def cmd_sync(app, a):
    targets = [app.index.source(i) for i in a.ids] if a.ids else app.index.sources()
    if not targets:
        print("ソースがありません（drivedex add ...）")
        return 1
    for s in targets:
        r = app.sync(s, full=a.full)
        print(f"[{s.id}] {s.label}: {r['mode']} 追加 {r['added']:,}・更新 {r['modified']:,}・削除 {r['deleted']:,}（計 {r['total']:,}）")


def cmd_search(app, a):
    hits = app.index.search(a.terms, a.limit)
    for h in hits:
        print(f"{(h['mtime'] or '')[:10]}  [{h['source_id']}]{h['path']}")
    print(f"-- {len(hits)} 件", file=sys.stderr)


def cmd_changes(app, a):
    rows = app.index.changes(_since(a.days))
    mark = {"added": "+", "modified": "~", "deleted": "-"}
    for r in rows:
        where = r["path"] if r["kind"] != "deleted" else r["name"]
        print(f"{r['at'][:16].replace('T', ' ')} {mark[r['kind']]} [{r['source_id']}]{where}")
    print(f"-- 直近 {a.days:g} 日の変更 {len(rows)} 件", file=sys.stderr)


def cmd_view(app, a):
    out = export(app.index, Path(a.out) if a.out else app.config.root / "drivedex.html")
    print(out)
    if not a.no_open:
        webbrowser.open(out.resolve().as_uri())


def cmd_get(app, a):
    src = app.index.source(a.id)
    row = app.index.find_path(a.id, a.path)
    if row is None or row["is_folder"]:
        print("ファイルが見つかりません（drivedex search で path を確かめてください）", file=sys.stderr)
        return 1
    from .providers import Item
    item = Item(row["item_id"], row["parent_id"], row["name"], False, mime=row["mime"])
    os.makedirs(a.out, exist_ok=True)
    dest = app.provider(src.provider, src.account).download(src.spec, item, a.out)
    print(dest)
    if a.md:
        if officemd.supported(dest):
            print(officemd.convert_file(dest))
        else:
            print(f"Markdown 変換の対象外です（docx / xlsx / pptx のみ）: {Path(dest).name}", file=sys.stderr)


def cmd_md(app, a):
    failed = 0
    for f in a.files:
        try:
            if a.stdout:
                sys.stdout.write(officemd.convert(f, max_rows=a.max_rows))
            else:
                if a.out:
                    os.makedirs(a.out, exist_ok=True)
                print(officemd.convert_file(f, a.out))
        except (ValueError, OSError) as e:
            print(f"失敗: {f}: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def build_parser():
    ap = argparse.ArgumentParser(prog="drivedex", description="SharePoint / OneDrive / Google ドライブの索引を手元に作る")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--home", help="設定・索引の置き場所（既定 %%LOCALAPPDATA%%\\drivedex）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="アプリ登録の client_id などを保存")
    cs = c.add_subparsers(dest="provider", required=True)
    m = cs.add_parser("ms")
    m.add_argument("--client-id", required=True)
    m.add_argument("--tenant", help="organizations（既定）/ common / テナントID")
    m.set_defaults(client_secret=None, client_secret_json=None)
    g = cs.add_parser("google")
    g.add_argument("--client-secret-json", help="Google Cloud でダウンロードした OAuth クライアントの JSON")
    g.add_argument("--client-id")
    g.add_argument("--client-secret")
    c.set_defaults(func=cmd_config)

    for name, func, hlp in (("login", cmd_login, "SSO でサインイン"), ("logout", cmd_logout, "トークンを削除")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("provider", choices=["ms", "google"])
        p.add_argument("--account", help="複数アカウントを使い分けるときの名前")
        p.set_defaults(func=func)

    p = sub.add_parser("add", help="索引に入れるドライブを登録")
    p.add_argument("provider", choices=["ms", "google"])
    p.add_argument("--account")
    p.add_argument("--me", action="store_true", help="[ms] 自分の OneDrive")
    p.add_argument("--site", help="[ms] SharePoint サイトの URL")
    p.add_argument("--library", help="[ms] ドキュメントライブラリ名（省略時はすべて）")
    p.add_argument("--my-drive", action="store_true", help="[google] マイドライブ")
    p.add_argument("--shared-drive", nargs="?", const="", help="[google] 共有ドライブ名（名前なしですべて）")
    p.set_defaults(func=cmd_add)

    sub.add_parser("sources", help="登録済みのドライブ").set_defaults(func=cmd_sources)
    p = sub.add_parser("remove", help="ドライブを索引から外す")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("sync", help="変更分を取得（初回は全件）")
    p.add_argument("ids", nargs="*", type=int)
    p.add_argument("--full", action="store_true", help="全件を取り直す")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("search", help="ファイル名で検索（スペース区切りで AND）")
    p.add_argument("terms", nargs="+")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("changes", help="最近の変更（追加・更新・削除）")
    p.add_argument("--days", type=float, default=7)
    p.set_defaults(func=cmd_changes)

    p = sub.add_parser("view", help="索引を1枚の HTML にして開く")
    p.add_argument("--out")
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("get", help="ファイルをダウンロード（Google ドキュメントは Office 形式に変換）")
    p.add_argument("id", type=int, help="ソース番号")
    p.add_argument("path", help="ソース内のパス（例 /議事録/2026/11月.docx）")
    p.add_argument("-o", "--out", default=".")
    p.add_argument("--md", action="store_true", help="docx / xlsx / pptx は Markdown も作る（AI に読ませる用）")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("md", help="手元の docx / xlsx / pptx を Markdown に変換（同じ場所に .md）")
    p.add_argument("files", nargs="+")
    p.add_argument("-o", "--out", help="出力フォルダ（省略時は元ファイルと同じ場所）")
    p.add_argument("--stdout", action="store_true", help="ファイルに書かず画面に出す")
    p.add_argument("--max-rows", type=int, default=5000, help="Excel の1シートあたりの最大行数")
    p.set_defaults(func=cmd_md)
    return ap


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    a = build_parser().parse_args(argv)
    app = App(Path(a.home) if a.home else None)
    try:
        return a.func(app, a) or 0
    except (NotConfigured, NotLoggedIn, ValueError, KeyError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 2
    finally:
        app.index.close()
