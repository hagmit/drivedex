import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from drivedex import http, oauth
from drivedex.cli import App, main
from drivedex.index import OTHER, Index
from drivedex.providers import Cursor, Deleted, Item, ResyncRequired, SourceSpec
from drivedex.providers import gdrive, msgraph
from drivedex.secure_store import ENCRYPTED, TokenStore
from drivedex.viewer import build_data, render


def F(i, p, name, folder=False, mtime="2026-11-01T00:00:00Z"):
    return Item(i, p, name, folder, 10, mtime, "user", f"https://x/{i}")


class IndexTest(unittest.TestCase):
    def setUp(self):
        self.idx = Index(":memory:")
        self.sid = self.idx.add_source("ms", "ms", SourceSpec("Team / Docs", "d1"))

    def test_full_then_delta(self):
        s = self.idx.apply(self.sid, [F("r", None, "root", True), F("a", "r", "設計", True),
                                      F("b", "a", "仕様書.xlsx"), Cursor("c1")], full=True)
        self.assertEqual(s["added"], 3)
        self.assertEqual(self.idx.changes("2000"), [])  # first sync is not logged
        self.assertEqual(self.idx.path_of(self.sid, "b"), "/設計/仕様書.xlsx")

        s = self.idx.apply(self.sid, [F("b", "a", "仕様書.xlsx", mtime="2026-11-02T00:00:00Z"),
                                      F("c", "r", "新規.docx"), Deleted("zzz"), Cursor("c2")], full=False)
        self.assertEqual((s["added"], s["modified"], s["deleted"]), (1, 1, 0))
        kinds = sorted(c["kind"] for c in self.idx.changes("2000"))
        self.assertEqual(kinds, ["added", "modified"])
        self.assertEqual(self.idx.source(self.sid).cursor, "c2")

        s = self.idx.apply(self.sid, [Deleted("c"), Cursor("c3")], full=False)
        self.assertEqual(s["deleted"], 1)
        self.assertIn("deleted", [c["kind"] for c in self.idx.changes("2000")])

    def test_full_resync_deletes_unseen_and_search(self):
        self.idx.apply(self.sid, [F("r", None, "root", True), F("x", "r", "a_b%.txt"),
                                  F("y", "r", "abc.txt"), Cursor("c1")], full=True)
        self.assertEqual([h["item_id"] for h in self.idx.search(["b%"])], ["x"])  # LIKE is escaped
        self.assertEqual(len(self.idx.search(["ab"])), 1)
        self.assertEqual(len(self.idx.search([".txt"])), 2)
        s = self.idx.apply(self.sid, [F("r", None, "root", True), F("y", "r", "abc.txt"), Cursor("c2")], full=True)
        self.assertEqual(s["deleted"], 1)
        self.assertEqual(self.idx.find_path(self.sid, "/abc.txt")["item_id"], "y")
        self.assertIsNone(self.idx.find_path(self.sid, "/nope.txt"))

    def test_rollback_on_error(self):
        def bad():
            yield F("r", None, "root", True)
            raise ResyncRequired()
        with self.assertRaises(ResyncRequired):
            self.idx.apply(self.sid, bad(), full=True)
        self.assertEqual(self.idx.items(self.sid), [])
        self.assertIsNone(self.idx.source(self.sid).synced_at)

    def test_orphan_path(self):
        self.idx.apply(self.sid, [F("r", None, "root", True), F("o", gdrive.ORPHAN, "共有.pdf"), Cursor("c")], full=True)
        self.assertEqual(self.idx.path_of(self.sid, "o"), f"/{OTHER}/共有.pdf")


class FakePages:
    """Serves canned JSON pages keyed by URL prefix, recording requests."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, params=None):
        self.calls.append((url, params))
        for prefix, resp in self.routes:
            if url.startswith(prefix):
                if isinstance(resp, Exception):
                    raise resp
                return resp(url, params) if callable(resp) else resp
        raise AssertionError("unexpected " + url)


def provider(cls, routes):
    p = cls({"client_id": "cid"}, store=None)
    p.get = FakePages(routes)
    return p


class GraphTest(unittest.TestCase):
    def test_parse_site_url(self):
        self.assertEqual(msgraph.parse_site_url("https://c.sharepoint.com/sites/team/Shared%20Documents/a"),
                         ("c.sharepoint.com", "/sites/team"))
        self.assertEqual(msgraph.parse_site_url("c.sharepoint.com"), ("c.sharepoint.com", "/"))

    def test_delta_paging(self):
        G = msgraph.GRAPH + "/drives/d1/root/delta"
        p = provider(msgraph.GraphProvider, [
            ("https://next", {"value": [{"id": "f", "name": "a.xlsx", "parentReference": {"id": "r"},
                                         "file": {"mimeType": "x"}, "lastModifiedBy": {"user": {"displayName": "U"}}},
                                        {"id": "g", "deleted": {}}],
                              "@odata.deltaLink": "https://delta?token=1"}),
            (G, {"value": [{"id": "r", "name": "root", "root": {}, "folder": {}}], "@odata.nextLink": "https://next"}),
        ])
        out = list(p.full(SourceSpec("x", "d1")))
        self.assertIsNone(out[0].parent_id)
        self.assertEqual((out[1].parent_id, out[1].modified_by, out[1].is_folder), ("r", "U", False))
        self.assertIsInstance(out[2], Deleted)
        self.assertEqual(out[3], Cursor("https://delta?token=1"))
        self.assertIsNone(p.get.calls[1][1])  # nextLink is used as-is

    def test_personal_account_scopes(self):
        work = msgraph.GraphProvider({"client_id": "c"}, store=None)
        personal = msgraph.GraphProvider({"client_id": "c", "tenant": "consumers"}, store=None)
        self.assertIn("Sites.Read.All", work.scopes)
        self.assertNotIn("Sites.Read.All", personal.scopes)
        self.assertIn("Files.Read.All", personal.scopes)
        self.assertIn("/consumers/oauth2/v2.0/authorize", personal.auth_url)
        with self.assertRaises(ValueError):
            personal.resolve(site="https://c.sharepoint.com/sites/t")

    def test_delta_gone_means_resync(self):
        p = provider(msgraph.GraphProvider, [("https://delta", http.HttpError(410, "resyncRequired", "u"))])
        with self.assertRaises(ResyncRequired):
            list(p.delta(SourceSpec("x", "d1"), "https://delta?token=1"))

    def test_resolve_site_filters_library(self):
        p = provider(msgraph.GraphProvider, [
            (msgraph.GRAPH + "/sites/s1/drives", {"value": [
                {"id": "d1", "name": "ドキュメント", "webUrl": "https://c/sites/t/Shared%20Documents", "driveType": "documentLibrary"},
                {"id": "d2", "name": "資料", "webUrl": "https://c/sites/t/Docs2", "driveType": "documentLibrary"}]}),
            (msgraph.GRAPH + "/sites/c.sharepoint.com:/sites/t", {"id": "s1", "displayName": "T"}),
        ])
        self.assertEqual(len(p.resolve(site="https://c.sharepoint.com/sites/t")), 2)
        specs = p.resolve(site="https://c.sharepoint.com/sites/t", library="Shared Documents")
        self.assertEqual([(s.drive_id, s.label) for s in specs], [("d1", "T / ドキュメント")])


class DriveTest(unittest.TestCase):
    def test_full_and_delta(self):
        A = gdrive.API
        pages = iter([
            {"files": [{"id": "f1", "name": "doc", "mimeType": "application/vnd.google-apps.document",
                        "parents": ["root1"], "modifiedTime": "t"}], "nextPageToken": "p2"},
            {"files": [{"id": "f2", "name": "sh.pdf", "mimeType": "application/pdf", "size": "5"}]},
        ])
        p = provider(gdrive.DriveProvider, [
            (A + "/changes/startPageToken", {"startPageToken": "s1"}),
            (A + "/files", lambda u, prm: next(pages)),
            (A + "/changes", {"changes": [{"fileId": "f1", "removed": True},
                                          {"fileId": "f3", "file": {"id": "f3", "name": "n", "trashed": True}},
                                          {"fileId": "f4", "file": {"id": "f4", "name": "m", "parents": ["root1"]}}],
                              "newStartPageToken": "s2"}),
        ])
        spec = SourceSpec("Google マイドライブ", gdrive.MY_DRIVE, "root1")
        out = list(p.full(spec))
        self.assertEqual((out[0].item_id, out[0].parent_id), ("root1", None))
        self.assertEqual(out[2].parent_id, gdrive.ORPHAN)
        self.assertEqual(out[2].size, 5)
        self.assertEqual(out[-1], Cursor("s1"))  # token taken BEFORE listing
        self.assertEqual(p.get.calls[1][1]["corpora"], "user")
        d = list(p.delta(spec, "s1"))
        self.assertEqual([type(x).__name__ for x in d], ["Deleted", "Deleted", "Item", "Cursor"])

    def test_shared_drive_params(self):
        p = provider(gdrive.DriveProvider, [])
        sc = p._scope(SourceSpec("x", "D9", "D9"))
        self.assertEqual((sc["corpora"], sc["driveId"], sc["supportsAllDrives"]), ("drive", "D9", "true"))

    def test_export_download(self):
        p = provider(gdrive.DriveProvider, [])
        p.access_token = lambda: "tok"
        with mock.patch.object(http, "download") as dl, tempfile.TemporaryDirectory() as d:
            dest = p.download(SourceSpec("x", "mydrive"), Item("f1", None, "議事録", False,
                              mime="application/vnd.google-apps.document"), d)
        self.assertTrue(dest.endswith("議事録.docx"))
        self.assertIn("/export?mimeType=", dl.call_args[0][0])


class FakeProvider:
    def __init__(self):
        self.full_calls = 0

    def full(self, spec):
        self.full_calls += 1
        yield F("r", None, "root", True)
        yield F("a", "r", "a.txt")
        yield Cursor("c-full")

    def delta(self, spec, cursor):
        raise ResyncRequired()
        yield  # pragma: no cover


class AppTest(unittest.TestCase):
    def test_sync_falls_back_to_full(self):
        with tempfile.TemporaryDirectory() as d:
            app = App(Path(d))
            sid = app.index.add_source("ms", "ms", SourceSpec("x", "d1"))
            fp = FakeProvider()
            self.assertEqual(app.sync(app.index.source(sid), provider=fp)["mode"], "full")
            r = app.sync(app.index.source(sid), provider=fp)
            self.assertEqual((r["mode"], fp.full_calls, r["total"]), ("full", 2, 2))
            app.index.close()

    def test_get_with_md(self):
        import zipfile

        class Downloader:
            def download(self, spec, item, dest_dir):
                dest = Path(dest_dir, item.name)
                with zipfile.ZipFile(dest, "w") as z:
                    z.writestr("word/document.xml",
                               '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                               '<w:body><w:p><w:r><w:t>議事録の本文</w:t></w:r></w:p></w:body></w:document>')
                return str(dest)

        with tempfile.TemporaryDirectory() as d:
            app = App(Path(d))
            sid = app.index.add_source("google", "google", SourceSpec("G", "mydrive", "r"))
            app.index.apply(sid, [F("r", None, "root", True), F("f", "r", "議事録.docx"), Cursor("c")], full=True)
            app.index.close()
            out = Path(d, "dl")
            with mock.patch("drivedex.providers.get", lambda *a, **k: Downloader()), \
                    mock.patch("sys.stdout"):
                self.assertEqual(main(["--home", d, "get", str(sid), "/議事録.docx", "-o", str(out), "--md"]), 0)
            self.assertIn("議事録の本文", (out / "議事録.docx.md").read_text("utf-8"))

    def test_cli_without_config(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stderr") as err:
            self.assertEqual(main(["--home", d, "login", "ms"]), 2)
            self.assertEqual(main(["--home", d, "sync"]), 1)
        self.assertIn("client_id", "".join(c.args[0] for c in err.write.call_args_list))


class HomeTest(unittest.TestCase):
    STORE = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_x\python.exe"
    PLAIN = r"C:\Program Files\Python313\python.exe"

    def test_store_python_avoids_appdata(self):
        from drivedex import config
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": r"C:\L"}, clear=False):
            os_env = __import__("os").environ
            os_env.pop("DRIVEDEX_HOME", None)
            self.assertTrue(config.is_store_python(self.STORE))
            self.assertFalse(config.is_store_python(self.PLAIN))
            self.assertEqual(config.home(self.STORE), Path.home() / ".drivedex")
            self.assertEqual(config.home(self.PLAIN), Path(r"C:\L") / "drivedex")

    def test_migrate_once(self):
        from drivedex import config
        with tempfile.TemporaryDirectory() as d:
            old, new = Path(d, "old"), Path(d, "new")
            (old / "tokens").mkdir(parents=True)
            (old / "config.json").write_text("{}", "utf-8")
            self.assertTrue(config.migrate(old, new))
            self.assertTrue((new / "tokens").is_dir())
            self.assertFalse(config.migrate(old, new))  # never overwrites
            self.assertFalse(config.migrate(None, Path(d, "x")))


class OAuthTest(unittest.TestCase):
    def test_pkce(self):
        v, c = oauth.pkce_pair()
        import base64, hashlib
        self.assertEqual(c, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode())
        self.assertGreaterEqual(len(v), 43)

    def _run(self, tamper_state=False):
        seen = {}

        def browser(url):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            seen.update(q)
            state = "evil" if tamper_state else q["state"]
            target = q["redirect_uri"].replace("localhost", "127.0.0.1") + "/?code=CODE&state=" + state

            def hit():
                time.sleep(0.2)
                urllib.request.urlopen(target, timeout=5).read()
            threading.Thread(target=hit, daemon=True).start()

        def post(url, data):
            seen["post"] = data
            return {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600}

        with mock.patch.object(http, "post_form", post):
            tok = oauth.authorize("https://auth", "https://token", "cid", ["a", "b"],
                                  open_browser=browser, timeout=10, echo=lambda *_: None)
        return tok, seen

    def test_authorize_roundtrip(self):
        tok, seen = self._run()
        self.assertEqual(tok["access_token"], "AT")
        self.assertGreater(tok["expires_at"], time.time() + 3000)
        self.assertEqual(seen["code_challenge_method"], "S256")
        self.assertEqual(seen["post"]["code"], "CODE")
        self.assertEqual(seen["post"]["redirect_uri"], seen["redirect_uri"])
        self.assertNotIn("client_secret", seen["post"])

    def test_state_mismatch(self):
        with self.assertRaises(oauth.AuthError):
            self._run(tamper_state=True)

    def test_refresh_keeps_refresh_token(self):
        with mock.patch.object(http, "post_form", lambda u, d: {"access_token": "N", "expires_in": 10}):
            new = oauth.refresh("https://t", "cid", {"refresh_token": "RT"})
        self.assertEqual((new["access_token"], new["refresh_token"]), ("N", "RT"))


class StoreTest(unittest.TestCase):
    def test_roundtrip_and_encrypted(self):
        with tempfile.TemporaryDirectory() as d:
            s = TokenStore(Path(d))
            s.save("ms", {"access_token": "SECRET-TOKEN"})
            self.assertEqual(s.load("ms")["access_token"], "SECRET-TOKEN")
            raw = next(Path(d, "tokens").iterdir()).read_bytes()
            if ENCRYPTED:
                self.assertNotIn(b"SECRET-TOKEN", raw)
            self.assertTrue(s.delete("ms"))
            self.assertIsNone(s.load("ms"))


class ViewerTest(unittest.TestCase):
    def test_build_and_escape(self):
        idx = Index(":memory:")
        sid = idx.add_source("google", "google", SourceSpec("G", "mydrive", "r"))
        idx.apply(sid, [F("r", None, "マイドライブ", True), F("a", "r", "</script><b>x", True),
                        F("b", "a", "b.txt"), F("o", gdrive.ORPHAN, "o.pdf"), Cursor("c")], full=True)
        idx.add_source("ms", "ms", SourceSpec("never synced", "d2"))
        data = build_data(idx)
        items = data["items"]
        by = {it[2]: n for n, it in enumerate(items)}
        self.assertEqual(items[by["G"]][1], -1)  # root renamed to the source label
        self.assertEqual(items[by["b.txt"]][1], by["</script><b>x"])
        self.assertEqual(items[by["o.pdf"]][1], by[OTHER])
        self.assertEqual(items[by["never synced"]][1], -1)
        html = render(data)
        self.assertEqual(html.count("</script>"), 2)  # only the two real closing tags
        payload = html.split('type="application/json">')[1].split("</script>")[0]
        self.assertEqual(json.loads(payload)["items"][by["</script><b>x"]][2], "</script><b>x")


if __name__ == "__main__":
    unittest.main()
