import datetime as dt
import json
import os
import sqlite3
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from drivedex import http, notify, schedule
from drivedex.cli import App, main
from drivedex.index import Index
from drivedex.providers import Cursor, Deleted, Item, NotLoggedIn, SourceSpec


def F(i, p, name, folder=False, mtime="2026-11-01T00:00:00Z"):
    return Item(i, p, name, folder, 10, mtime, "山田", f"https://x/{i}")


TREE = [F("r", None, "root", True), F("d", "r", "設計", True), F("e", "r", "営業", True),
        F("a", "d", "仕様書.xlsx"), F("b", "e", "見積.xlsx"), F("c", "d", "古い.docx")]


class Script:
    """Provider whose delta() replays one scripted batch per sync."""

    def __init__(self, *batches):
        self.batches = list(batches)

    def full(self, spec):
        yield from TREE
        yield Cursor("c0")

    def delta(self, spec, cursor):
        batch = self.batches.pop(0) if self.batches else []
        if isinstance(batch, Exception):
            raise batch
        yield from batch
        yield Cursor(cursor + "+")


class NotifyRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name))
        self.sid = self.app.index.add_source("google", "google", SourceSpec("G", "mydrive", "r"))
        self.app.sync(self.app.index.source(self.sid), provider=Script())  # first listing: not a change
        self.toasts = []

    def tearDown(self):
        self.app.index.close()
        self.tmp.cleanup()

    def run_once(self, provider):
        with mock.patch.object(App, "provider", lambda *a, **k: provider), mock.patch("sys.stdout"):
            return notify.run(self.app, notifier=lambda *a: self.toasts.append(a))

    def test_notifies_only_new_changes_once(self):
        p = Script([F("a", "d", "仕様書.xlsx", mtime="2026-11-02T00:00:00Z"), F("n", "e", "新規.pptx")], [])
        self.assertEqual(self.run_once(p), 0)
        title, lines, launch = self.toasts[0]
        self.assertEqual(title, "drivedex: 2 件の変更")
        self.assertTrue(launch.startswith("file:") and launch.endswith("changes.html"))
        self.assertEqual(sorted(lines), ["+ 営業/新規.pptx", "~ 設計/仕様書.xlsx"])
        time.sleep(1.1)  # changes are stamped to the second
        self.assertEqual(self.run_once(p), 0)
        self.assertEqual(len(self.toasts), 1)  # nothing new: no second toast
        root = Path(self.tmp.name)
        log = (root / notify.LOG).read_text("utf-8").splitlines()
        self.assertEqual(len(log), 2)
        self.assertTrue(any(l.endswith("~ [1]/設計/仕様書.xlsx") for l in log))
        self.assertIn("新規.pptx", (root / notify.REPORT).read_text("utf-8"))  # still in the 7-day report

    def test_folder_filter_includes_deletions(self):
        self.app.config.data["notify"] = {"folders": ["/設計"]}
        p = Script([Deleted("c"), F("n", "e", "新規.pptx"), F("x", "r", "設計書.txt")])
        self.run_once(p)
        self.assertEqual(self.toasts[0][1], ["- 設計/古い.docx"])  # /設計書.txt is not inside /設計

    def test_many_changes_are_summarized(self):
        self.run_once(Script([F(f"n{i}", "d", f"{i}.txt") for i in range(5)]))
        title, lines, _ = self.toasts[0]
        self.assertEqual(title, "drivedex: 5 件の変更")
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("ほか 3 件"))

    def test_errors_are_told_once_and_other_sources_still_sync(self):
        self.app.index.add_source("ms", "work", SourceSpec("Team", "d9"))

        class Broken:
            def full(self, spec):
                raise NotLoggedIn("未ログインです")
                yield  # pragma: no cover

        good = Script([F("n", "d", "新規.txt")], [])

        def pick(app, name, account=None):
            return good if name == "google" else Broken()
        with mock.patch.object(App, "provider", pick), mock.patch("sys.stdout"):
            code1 = notify.run(self.app, notifier=lambda *a: self.toasts.append(a))
            code2 = notify.run(self.app, notifier=lambda *a: self.toasts.append(a))
        self.assertEqual((code1, code2), (1, 1))
        titles = [t[0] for t in self.toasts]
        self.assertEqual(titles, ["drivedex: 1 件の変更", "drivedex: 取得できなかったドライブがあります"])
        self.assertIn("drivedex login ms --account work", self.toasts[1][1][0])
        state = json.loads(Path(self.tmp.name, notify.STATE).read_text("utf-8"))
        self.assertIn("サインインし直して", state["error"])

    def test_skips_while_another_run_holds_the_lock(self):
        lock = Path(self.tmp.name, notify.LOCK)
        lock.write_text("123")
        self.assertEqual(self.run_once(Script([F("n", "d", "x.txt")])), 0)
        self.assertEqual(self.toasts, [])
        old = time.time() - 3 * 3600
        os.utime(lock, (old, old))  # a crashed run's lock is taken over
        self.run_once(Script([F("n", "d", "x.txt")]))
        self.assertEqual(len(self.toasts), 1)
        self.assertFalse(lock.exists())

    def test_report_escapes_names(self):
        self.run_once(Script([F("n", "d", "<script>alert(1)</script>.txt")]))
        page = Path(self.tmp.name, notify.REPORT).read_text("utf-8")
        self.assertNotIn("<script>alert", page)
        self.assertIn("&lt;script&gt;", page)


class NotifyPartsTest(unittest.TestCase):
    def test_folders(self):
        self.assertEqual(notify.normalize_folder("設計\\2026/"), "/設計/2026")
        self.assertTrue(notify.in_folders("/設計/a.txt", ["/設計"]))
        self.assertFalse(notify.in_folders("/設計書.txt", ["/設計"]))
        self.assertTrue(notify.in_folders("/x", []))
        self.assertTrue(notify.in_folders("/x", ["/"]))

    def test_toast_xml_is_escaped(self):
        x = notify.toast_xml('a<b>&"', ["c"], "file:///C:/d&e.html")
        root = ET.fromstring(x)
        self.assertEqual(root.get("launch"), "file:///C:/d&e.html")
        self.assertEqual([t.text for t in root.iter("text")], ['a<b>&"', "c"])

    def test_describe_error(self):
        class S:
            id, label, provider, account = 2, "G", "google", "google"
        self.assertIn("drivedex login google）", notify.describe_error(S, http.HttpError(400, '{"error":"invalid_grant"}', "u")))
        self.assertIn("HttpError", notify.describe_error(S, http.HttpError(503, "busy", "u")))

    def test_old_index_gets_path_column(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d, "index.db")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE changes(source_id INTEGER NOT NULL, item_id TEXT NOT NULL, "
                        "kind TEXT NOT NULL, name TEXT, at TEXT NOT NULL)")
            con.execute("INSERT INTO changes VALUES(1,'x','deleted','古い.docx','2026-11-01T00:00:00+00:00')")
            con.commit()
            con.close()
            idx = Index(db)
            sid = idx.add_source("google", "google", SourceSpec("G", "mydrive", "r"))
            self.assertEqual(sid, 1)
            self.assertEqual(idx.changes("2000")[0]["path"], "古い.docx")  # old rows fall back to the name
            idx.close()


class ScheduleTest(unittest.TestCase):
    def test_task_xml(self):
        home = Path(r"C:\Users\山田 太郎\.drivedex")
        x = schedule.task_xml(r"C:\Python313\pythonw.exe", schedule.arguments(home), 60, dt.datetime(2026, 11, 16, 9, 1))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        root = ET.fromstring(x.split("\n", 1)[1])
        self.assertEqual(root.find(".//t:Interval", ns).text, "PT60M")
        self.assertEqual(root.find(".//t:StartBoundary", ns).text, "2026-11-16T09:01:00")
        self.assertEqual(root.find(".//t:LogonType", ns).text, "InteractiveToken")
        self.assertEqual(root.find(".//t:MultipleInstancesPolicy", ns).text, "IgnoreNew")
        self.assertEqual(root.find(".//t:Command", ns).text, r"C:\Python313\pythonw.exe")
        self.assertEqual(root.find(".//t:Arguments", ns).text,
                         r'-m drivedex --home "C:\Users\山田 太郎\.drivedex" sync --notify')

    def test_pythonw_choice(self):
        with tempfile.TemporaryDirectory() as d:
            exe = Path(d, "python.exe")
            exe.touch()
            self.assertEqual(schedule.pythonw(str(exe), {}), str(exe))  # no pythonw next to it
            Path(d, "pythonw.exe").touch()
            self.assertEqual(schedule.pythonw(str(exe), {}), str(Path(d, "pythonw.exe")))
        store = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.13_x\python.exe"
        self.assertTrue(schedule.pythonw(store, {"LOCALAPPDATA": r"C:\L"}).endswith(".exe"))
        self.assertIn("pythonw3.", schedule.pythonw(store, {"LOCALAPPDATA": r"C:\L"}))

    def test_cron_line_and_limits(self):
        args = ["-m", "drivedex", "sync", "--notify"]
        self.assertTrue(schedule.cron_line(15, "/usr/bin/python3", args).startswith("*/15 * * * * /usr/bin/python3"))
        self.assertTrue(schedule.cron_line(60, "py", args).startswith("0 * * * * "))
        self.assertTrue(schedule.cron_line(180, "py", args).startswith("0 */3 * * * "))
        for bad in (0, 4, 1441):
            with self.assertRaises(ValueError):
                schedule.check_every(bad)

    def test_cli_saves_folders(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(schedule, "install") as inst, \
                mock.patch("sys.stdout"):
            self.assertEqual(main(["--home", d, "schedule", "on", "--every", "30", "--folder", "設計/", "--folder", "/営業"]), 0)
            cfg = json.loads(Path(d, "config.json").read_text("utf-8"))
            self.assertEqual(cfg["notify"], {"folders": ["/営業", "/設計"], "every": 30})
            self.assertEqual(inst.called, os.name == "nt")
            main(["--home", d, "schedule", "on"])  # no --folder: keeps the filter
            self.assertEqual(json.loads(Path(d, "config.json").read_text("utf-8"))["notify"]["folders"], ["/営業", "/設計"])
            main(["--home", d, "schedule", "on", "--all-folders"])
            self.assertEqual(json.loads(Path(d, "config.json").read_text("utf-8"))["notify"]["folders"], [])

    def test_notify_rejects_ids(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("sys.stderr"):
            self.assertEqual(main(["--home", d, "sync", "1", "--notify"]), 2)

    @unittest.skipUnless(os.name == "nt" and os.environ.get("DRIVEDEX_TEST_SCHTASKS") == "1",
                         "registers a real task: CI on Windows only")
    def test_real_task_scheduler(self):
        with tempfile.TemporaryDirectory() as d:
            try:
                schedule.install(Path(d), 60)
                out = schedule.status()
                self.assertIsNotNone(out)
                self.assertIn("drivedex", out)
            finally:
                schedule.remove()
            self.assertIsNone(schedule.status())


if __name__ == "__main__":
    unittest.main()
