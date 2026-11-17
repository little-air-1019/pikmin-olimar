"""SQLite 持久化 + 單一執行鎖。pending 派遣在按 GO『之前』寫入（write-ahead），任何 pending 都阻擋新的 GO。"""
from __future__ import annotations

import fcntl
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class LockBusy(RuntimeError):
    pass


class PendingBlocks(RuntimeError):
    pass


def day_of(ts: float, tz: str) -> str:
    """遊戲當地日期（午夜重設）。"""
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%Y-%m-%d")


class Store:
    def __init__(self, root: Path, tz: str = "Asia/Taipei", clock=time.time):
        self.root, self.tz, self.clock = Path(root), tz, clock
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "olimar.db", isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS dispatch(
          id INTEGER PRIMARY KEY, day TEXT, created_at REAL, mushroom TEXT, color TEXT,
          status TEXT, expected_end REAL, note TEXT, evidence TEXT, updated_at REAL);
        CREATE TABLE IF NOT EXISTS display(day TEXT, ts REAL, remaining INTEGER);
        CREATE TABLE IF NOT EXISTS sighting(seen_at REAL, end_at REAL, note TEXT);
        """)

    @contextmanager
    def run_lock(self):
        f = open(self.root / "run.lock", "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise LockBusy("另一個 olimar 程序正在執行")
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    def today(self) -> str:
        return day_of(self.clock(), self.tz)

    def record_display(self, remaining: int) -> None:
        self.db.execute("INSERT INTO display VALUES(?,?,?)", (self.today(), self.clock(), remaining))

    def record_sighting(self, end_at: float, note: str = "") -> None:
        """看過的蘑菇預計結束時間（含沒去搶的滿員蘑菇），用來預先啟動重生附近的快速輪詢。純估計。"""
        self.db.execute("INSERT INTO sighting VALUES(?,?,?)", (self.clock(), end_at, note))

    def upcoming_ends(self, since: float) -> list[float]:
        return [r[0] for r in self.db.execute("SELECT DISTINCT CAST(end_at AS INT) FROM sighting WHERE end_at >= ? ORDER BY end_at", (since,))]

    def pending(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM dispatch WHERE status='pending' ORDER BY id").fetchall()

    def begin_dispatch(self, mushroom: str, color: str | None, evidence: str = "") -> int:
        """write-ahead：BEGIN IMMEDIATE 確保檢查與寫入原子；有 pending 就拒絕。"""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.pending():
                raise PendingBlocks("存在 pending 派遣，請先 `olimar reconcile`")
            now = self.clock()
            cur = self.db.execute(
                "INSERT INTO dispatch(day,created_at,mushroom,color,status,evidence,updated_at) VALUES(?,?,?,?,?,?,?)",
                (self.today(), now, mushroom, color, "pending", evidence, now))
            self.db.execute("COMMIT")
            return cur.lastrowid
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def finish(self, did: int, status: str, note: str = "", expected_end: float | None = None) -> None:
        assert status in ("confirmed", "failed", "pending")
        self.db.execute("UPDATE dispatch SET status=?, note=?, expected_end=?, updated_at=? WHERE id=?",
                        (status, note, expected_end, self.clock(), did))

    def reconcile(self, did: int, status: str, note: str = "") -> bool:
        """只允許改 pending 的派遣；回傳是否真的更新了一列。"""
        assert status in ("confirmed", "failed")
        cur = self.db.execute("UPDATE dispatch SET status=?, note=?, updated_at=? WHERE id=? AND status='pending'",
                              (status, note or f"人工 reconcile → {status}", self.clock(), did))
        return cur.rowcount == 1

    def confirmed_today(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM dispatch WHERE status='confirmed' AND day=?",
                               (self.today(),)).fetchone()[0]

    def last_expected_end(self) -> float | None:
        r = self.db.execute("SELECT expected_end FROM dispatch WHERE status='confirmed' AND expected_end IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1").fetchone()
        return r[0] if r else None

    def recent(self, n: int = 10) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM dispatch ORDER BY id DESC LIMIT ?", (n,)).fetchall()
