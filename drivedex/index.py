"""Local SQLite index of drive items. Nothing leaves the machine."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .providers import Cursor, Deleted, Item, SourceSpec

OTHER = "(他の場所)"  # items whose parent chain does not reach the source root

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, account TEXT NOT NULL, label TEXT NOT NULL,
  drive_id TEXT NOT NULL, root_id TEXT, cursor TEXT, synced_at TEXT,
  UNIQUE(provider, account, drive_id));
CREATE TABLE IF NOT EXISTS items(
  source_id INTEGER NOT NULL, item_id TEXT NOT NULL, parent_id TEXT, name TEXT NOT NULL,
  is_folder INTEGER NOT NULL, size INTEGER, mtime TEXT, modified_by TEXT, web_url TEXT, mime TEXT,
  PRIMARY KEY(source_id, item_id));
CREATE INDEX IF NOT EXISTS items_parent ON items(source_id, parent_id);
CREATE TABLE IF NOT EXISTS changes(
  source_id INTEGER NOT NULL, item_id TEXT NOT NULL, kind TEXT NOT NULL, name TEXT, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS changes_at ON changes(at);
"""

COLS = "item_id,parent_id,name,is_folder,size,mtime,modified_by,web_url,mime"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Source:
    def __init__(self, row: sqlite3.Row):
        self.id = row["id"]
        self.provider = row["provider"]
        self.account = row["account"]
        self.label = row["label"]
        self.cursor = row["cursor"]
        self.synced_at = row["synced_at"]
        self.spec = SourceSpec(row["label"], row["drive_id"], row["root_id"])


class Index:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), isolation_level=None)  # explicit transactions
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    # sources ------------------------------------------------------------
    def add_source(self, provider: str, account: str, spec: SourceSpec) -> int:
        self.db.execute(
            "INSERT INTO sources(provider,account,label,drive_id,root_id) VALUES(?,?,?,?,?) "
            "ON CONFLICT(provider,account,drive_id) DO UPDATE SET label=excluded.label",
            (provider, account, spec.label, spec.drive_id, spec.root_id))
        return self.db.execute("SELECT id FROM sources WHERE provider=? AND account=? AND drive_id=?",
                               (provider, account, spec.drive_id)).fetchone()[0]

    def sources(self) -> list[Source]:
        return [Source(r) for r in self.db.execute("SELECT * FROM sources ORDER BY id")]

    def source(self, source_id: int) -> Source:
        r = self.db.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if not r:
            raise KeyError(f"source {source_id} がありません")
        return Source(r)

    def remove_source(self, source_id: int) -> None:
        self.db.execute("BEGIN")
        for t in ("items", "changes"):
            self.db.execute(f"DELETE FROM {t} WHERE source_id=?", (source_id,))
        self.db.execute("DELETE FROM sources WHERE id=?", (source_id,))
        self.db.execute("COMMIT")

    # sync ---------------------------------------------------------------
    def apply(self, source_id: int, stream, *, full: bool) -> dict:
        """Apply a provider stream in one transaction. Changes are logged only
        after the first successful sync so the initial listing is not 'added'."""
        src = self.source(source_id)
        record = src.synced_at is not None
        stats = {"added": 0, "modified": 0, "deleted": 0, "total": 0}
        seen: set[str] = set()
        cursor = None
        at = now_iso()
        cur = self.db.cursor()
        try:
            cur.execute("BEGIN")
            for obj in stream:
                if isinstance(obj, Cursor):
                    cursor = obj.value
                elif isinstance(obj, Deleted):
                    self._delete(cur, source_id, obj.item_id, record, at, stats)
                elif isinstance(obj, Item):
                    seen.add(obj.item_id)
                    self._upsert(cur, source_id, obj, record, at, stats)
            if cursor is None:
                raise RuntimeError("provider stream ended without a cursor")
            if full:
                existing = [r[0] for r in cur.execute(
                    "SELECT item_id FROM items WHERE source_id=?", (source_id,))]
                for item_id in existing:
                    if item_id not in seen:
                        self._delete(cur, source_id, item_id, record, at, stats)
            cur.execute("UPDATE sources SET cursor=?, synced_at=? WHERE id=?", (cursor, at, source_id))
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        stats["total"] = self.db.execute(
            "SELECT COUNT(*) FROM items WHERE source_id=?", (source_id,)).fetchone()[0]
        return stats

    def _upsert(self, cur, sid, it: Item, record, at, stats):
        old = cur.execute("SELECT mtime,name,parent_id FROM items WHERE source_id=? AND item_id=?",
                          (sid, it.item_id)).fetchone()
        cur.execute(f"INSERT OR REPLACE INTO items(source_id,{COLS}) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (sid, it.item_id, it.parent_id, it.name, int(it.is_folder), it.size, it.mtime,
                     it.modified_by, it.web_url, it.mime))
        if old is None:
            kind = "added"
        elif (old["mtime"], old["name"], old["parent_id"]) != (it.mtime, it.name, it.parent_id):
            kind = "modified"
        else:
            return
        stats[kind] += 1
        if record and not it.is_folder:
            cur.execute("INSERT INTO changes VALUES(?,?,?,?,?)", (sid, it.item_id, kind, it.name, at))

    def _delete(self, cur, sid, item_id, record, at, stats):
        old = cur.execute("SELECT name,is_folder FROM items WHERE source_id=? AND item_id=?",
                          (sid, item_id)).fetchone()
        if old is None:
            return
        cur.execute("DELETE FROM items WHERE source_id=? AND item_id=?", (sid, item_id))
        stats["deleted"] += 1
        if record and not old["is_folder"]:
            cur.execute("INSERT INTO changes VALUES(?,?,?,?,?)", (sid, item_id, "deleted", old["name"], at))

    # queries ------------------------------------------------------------
    def items(self, source_id: int) -> list[sqlite3.Row]:
        return self.db.execute(f"SELECT {COLS} FROM items WHERE source_id=?", (source_id,)).fetchall()

    def path_of(self, source_id: int, item_id: str, _cache=None) -> str:
        names, cur, guard = [], item_id, 0
        while cur is not None and guard < 256:
            r = self.db.execute("SELECT parent_id,name FROM items WHERE source_id=? AND item_id=?",
                                (source_id, cur)).fetchone()
            if r is None:
                names.append(OTHER)
                break
            if r["parent_id"] is None:  # reached the root; root name is not part of the path
                break
            names.append(r["name"])
            cur, guard = r["parent_id"], guard + 1
        return "/" + "/".join(reversed(names))

    def search(self, terms: list[str], limit: int = 50) -> list[dict]:
        where = " AND ".join("name LIKE ? ESCAPE '\\'" for _ in terms) or "1"
        args = ["%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for t in terms]
        rows = self.db.execute(
            f"SELECT i.source_id, s.label, {COLS} FROM items i JOIN sources s ON s.id=i.source_id "
            f"WHERE parent_id IS NOT NULL AND {where} ORDER BY mtime DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**dict(r), "path": self.path_of(r["source_id"], r["item_id"])} for r in rows]

    def find_path(self, source_id: int, path: str) -> sqlite3.Row | None:
        root = self.db.execute(f"SELECT {COLS} FROM items WHERE source_id=? AND parent_id IS NULL",
                               (source_id,)).fetchone()
        node = root
        for part in [p for p in path.split("/") if p]:
            if node is None:
                return None
            node = self.db.execute(f"SELECT {COLS} FROM items WHERE source_id=? AND parent_id=? AND name=?",
                                   (source_id, node["item_id"], part)).fetchone()
        return node

    def changes(self, since: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT c.source_id, s.label, c.item_id, c.kind, c.name, c.at FROM changes c "
            "JOIN sources s ON s.id=c.source_id WHERE c.at >= ? ORDER BY c.at DESC, c.name", (since,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["path"] = self.path_of(r["source_id"], r["item_id"]) if r["kind"] != "deleted" else "(削除)"
            out.append(d)
        return out
