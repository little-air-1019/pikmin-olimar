import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from olimar import domain, vision
from olimar.controller import Config, Controller
from olimar.device import GuardError, check_click, px_to_pt
from olimar.domain import Mushroom, decide_detail
from olimar.scheduler import poll_interval
from olimar.store import LockBusy, PendingBlocks, Store, day_of
from olimar.vision import Line, Observation

from .fakes import W, H, WIN

ROOT = Path(__file__).parent.parent
FIX = Path(__file__).parent / "fixtures"  # 去識別化的 OCR JSON（玩家名／地點已換成 ＊）
EVID = ROOT / "runtime" / "evidence"  # 真實截圖（含私人資訊，gitignored）；不存在時相關測試 skip

# 由真實截圖量到的顏色統計（GO 圓形、GO 上半）
REAL_GO = {
    "disabled": ({"red": 0.008, "orange": 0.016, "white": 0.566}, {"orange": 0.017}),
    "ticket": ({"red": 0.106, "orange": 0.057, "white": 0.19}, {"orange": 0.176}),
}


def replay(name, colors=None, win=(348, 766)):
    d = json.loads((FIX / f"{name}.ocr.json").read_text())
    return Observation([Line(**l) for l in d["lines"]], d["width"], d["height"], 1, win[0], win[1], 0.0,
                       colors=colors or (lambda r: None))


def go_colors(kind):
    circ, top = REAL_GO[kind]
    return lambda r: (top if r[3] < 0.06 * 1280 else circ) | {"total": 1}


class RecordedScreens(unittest.TestCase):
    """真實 OCR 輸出（已去識別化）的分類與解析。"""

    def test_empty_team_screen_is_classified_by_header(self):
        o = replay("selection-empty", go_colors("disabled"))
        self.assertEqual(vision.classify(o), "team")  # 空隊伍：沒有 GO 文字也要認得
        self.assertEqual(vision.team_counts(o), (0, 40))
        self.assertEqual(vision.go_state(o)["state"], "disabled")

    def test_ticket_on_go_blocks(self):
        o = replay("selection-full-ticket-go", go_colors("ticket"))
        self.assertEqual((vision.classify(o), vision.team_counts(o)), ("team", (40, 40)))
        self.assertEqual(vision.go_state(o)["state"], "ticket")
        r = Controller(None, None, Config()).plan(o)
        self.assertEqual(r.status, "stopped")
        self.assertIn("券", r.detail)

    def test_detail_full_vs_free(self):
        full = replay("full-detail", lambda r: {"orange": 0.078})
        free = replay("free-detail", lambda r: {"orange": 0.0})
        self.assertEqual(vision.ticket_state(full), "ticket")
        self.assertEqual(vision.ticket_state(free), "free")
        m = vision.mushroom_of(free)
        self.assertEqual((m.size, m.kind, m.min_team), ("一般", "華麗", 10))
        self.assertEqual(decide_detail(m, remaining=2, ticket="free", players_seen=0).action, "join")
        self.assertEqual(decide_detail(m, remaining=2, ticket="ticket", players_seen=4).action, "skip")

    def test_list(self):
        o = replay("list-initial")
        self.assertEqual((vision.classify(o), vision.remaining_free(o)), ("list", 3))
        self.assertIsNone(vision.team_counts(o))  # 『蘑菇：1/12』不可誤判為隊伍量

    def test_ticket_text_off_list_is_dialog(self):
        o = Observation([Line("使用蘑菇券", 1, 0, 0, 10, 10)], W, H, 1, 348, 766, 0.0)
        self.assertEqual(vision.classify(o), "ticket_dialog")

    def test_go_unknown_without_evidence_never_plain(self):
        o = replay("selection-empty", lambda r: None)
        self.assertEqual(vision.go_state(o)["state"], "unknown")
        o = replay("selection-empty", lambda r: {"red": 0.04, "orange": 0.03, "white": 0.3})  # 介於門檻之間
        self.assertEqual(vision.go_state(o)["state"], "unknown")


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc") and (EVID / "selection-empty.png").exists(),
                     "需要 macOS + swiftc + runtime/evidence 的真實截圖（含私人資訊，不在 repo 內）")
class RealScreenshots(unittest.TestCase):
    """真實 PNG → Vision OCR／顏色 → 分類。與 fake 不同：這裡是實機截圖。"""

    def obs(self, name):
        from olimar.device import offline_observation
        return offline_observation(str(EVID / f"{name}.png"))

    def test_selection_empty(self):
        o = self.obs("selection-empty")
        self.assertEqual((vision.classify(o), vision.team_counts(o)), ("team", (0, 40)))
        self.assertEqual(vision.go_state(o)["state"], "disabled")
        self.assertEqual([d["color"] for d in vision.filter_dots(o)], ["紅", "黃", "藍", "紫"])  # 第 5 顆被裁切，不點

    def test_race_frame_blocks_go(self):
        o = self.obs("selection-full-ticket-go")
        self.assertEqual((vision.classify(o), vision.team_counts(o)), ("team", (40, 40)))
        g = vision.go_state(o)
        self.assertEqual(g["state"], "ticket", g)
        self.assertEqual(Controller(None, None, Config()).plan(o).status, "stopped")

    def test_details(self):
        self.assertEqual(vision.ticket_state(self.obs("full-detail")), "ticket")
        self.assertEqual(vision.ticket_state(self.obs("detail-after-race")), "ticket")
        free = self.obs("free-detail")
        self.assertEqual(vision.ticket_state(free), "free")
        self.assertEqual(Controller(None, None, Config()).plan(free).status, "planned")

    def test_list_and_scale_per_axis(self):
        o = self.obs("list-initial")
        self.assertEqual((vision.classify(o), vision.remaining_free(o)), ("list", 3))
        self.assertEqual((o.shot_w, o.shot_h), (696, 1532))


class Domain(unittest.TestCase):
    def test_kind_colors(self):
        self.assertEqual(domain.color_for("毒"), "白")
        self.assertEqual(domain.color_for("電"), "黃")
        self.assertEqual(domain.color_for("水晶"), "岩")
        self.assertEqual(domain.color_for("水"), "藍")
        self.assertIsNone(domain.color_for("華麗"))  # 活動蘑菇不硬套規則
        self.assertIsNone(domain.color_for("水果"))  # 未知種類不模糊比對

    def test_min_team(self):
        self.assertEqual([domain.MIN_TEAM[k] for k in ("小型", "一般", "大型", "巨大")], [1, 10, 15, 20])

    def test_decide_guards(self):
        m = Mushroom("大型", "毒")
        ok = decide_detail(m, remaining=2, ticket="free", players_seen=3)
        self.assertEqual((ok.action, ok.mushroom.color), ("join", "白"))
        self.assertEqual(decide_detail(m, remaining=0, ticket="free", players_seen=0).action, "stop")
        self.assertEqual(decide_detail(m, remaining=None, ticket="free", players_seen=0).action, "stop")
        self.assertEqual(decide_detail(m, remaining=2, ticket="free", players_seen=5).action, "skip")
        self.assertEqual(decide_detail(m, remaining=2, ticket="unknown", players_seen=0).action, "skip")
        self.assertEqual(decide_detail(Mushroom(None, None), remaining=2, ticket="free", players_seen=0).action, "skip")


class DeviceGuards(unittest.TestCase):
    def obs(self, w=W, h=H, **kw):
        import time
        return Observation([], w, h, 1, 348, 766, kw.get("t", time.time()), offline=kw.get("offline", False))

    def test_conversion_is_per_axis_not_fixed_scale(self):
        self.assertEqual(px_to_pt(self.obs(), 696, 1532), (348, 766))
        self.assertEqual(px_to_pt(self.obs(581, 1280), 581, 1280), (348, 766))
        x, y = px_to_pt(self.obs(494, 1088), 247, 544)
        self.assertAlmostEqual(x, 174)
        self.assertAlmostEqual(y, 383)

    def test_ok_and_refusals(self):
        import time
        now = time.time()
        self.assertEqual(check_click(self.obs(), WIN, 100, 200, now=now, max_age=10), (50, 100))
        for bad, kw in [
            ("id", dict(cur={**WIN, "id": 2})), ("size", dict(cur={**WIN, "width": 300})),
            ("minimized", dict(cur={**WIN, "isMinimized": True})),
            ("out-x", dict(px=W + 1)), ("out-y", dict(py=-1)), ("edge", dict(px=0)),
            ("stale", dict(now=now + 60)), ("offline", dict(obs=self.obs(offline=True))),
        ]:
            with self.subTest(bad), self.assertRaises(GuardError):
                check_click(kw.get("obs", self.obs()), kw.get("cur", WIN), kw.get("px", 100), kw.get("py", 200),
                            now=kw.get("now", now), max_age=10)


class StoreTests(unittest.TestCase):
    def test_midnight_rollover_and_pending_persists(self):
        tz = ZoneInfo("Asia/Taipei")
        t1 = datetime(2026, 9, 29, 23, 59, tzinfo=tz).timestamp()
        t2 = datetime(2026, 9, 30, 0, 1, tzinfo=tz).timestamp()
        self.assertNotEqual(day_of(t1, "Asia/Taipei"), day_of(t2, "Asia/Taipei"))
        with tempfile.TemporaryDirectory() as d:
            now = [t1]
            s = Store(Path(d), clock=lambda: now[0])
            did = s.begin_dispatch("一般 毒", "白")
            now[0] = t2
            self.assertEqual(len(Store(Path(d), clock=lambda: now[0]).pending()), 1)  # 重啟＋跨日仍 pending
            with self.assertRaises(PendingBlocks):
                s.begin_dispatch("x", None)
            self.assertTrue(s.reconcile(did, "confirmed"))
            self.assertEqual(s.confirmed_today(), 0)  # 新的一天重新計數（該筆記在前一天）
            self.assertGreater(s.begin_dispatch("y", None), did)

    def test_reconcile_only_touches_pending(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d))
            did = s.begin_dispatch("x", None)
            s.finish(did, "confirmed", "ok")
            self.assertFalse(s.reconcile(did, "failed"))  # 已確認的不可被改
            self.assertFalse(s.reconcile(999, "failed"))  # 不存在
            self.assertEqual(s.confirmed_today(), 1)

    def test_run_lock(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Store(Path(d)), Store(Path(d))
            with a.run_lock():
                with self.assertRaises(LockBusy), b.run_lock():
                    pass
            with b.run_lock():
                pass


class Countdown(unittest.TestCase):
    def cd(self, text):
        return vision.countdown_seconds(Observation([Line(text, 1, 0, 0, 1, 1)], W, H, 1, 348, 766, 0.0))

    def test_formats(self):
        self.assertEqual(self.cd("剩下3分38秒"), 218)
        self.assertEqual(self.cd("剩下59分 54秒"), 3594)
        self.assertEqual(self.cd("剩下2小時31分37秒"), 9097)
        self.assertEqual(self.cd("預估 剩下35分14秒"), 2114)
        self.assertEqual(self.cd("剩下37分18利"), 2238)  # 實機 OCR 把『秒』讀成『利』
        self.assertIsNone(self.cd("今天還剩下3次"))


class OrcaObservationAge(unittest.TestCase):
    def test_age_counts_from_before_capture(self):
        import time
        from olimar.device import OrcaDevice
        dev = OrcaDevice()
        dev._state = lambda *a: {"screenshot": {"width": 696, "height": 1532, "path": "x.png"},
                                 "snapshot": {"window": {"id": 1, "width": 348, "height": 766}}}
        orig = vision.ocr
        vision.ocr = lambda *a, **k: (time.sleep(0.3), (696, 1532, []))[1]  # OCR 很慢
        try:
            o = dev.observe()
        finally:
            vision.ocr = orig
        self.assertGreaterEqual(time.time() - o.taken_at, 0.3)  # 年齡包含 OCR 耗時
        with self.assertRaises(GuardError):
            check_click(o, WIN, 100, 200, now=time.time(), max_age=0.2)


class Scheduler(unittest.TestCase):
    def test_adaptive_poll_near_respawn(self):
        c = Config()
        n = 10_000.0
        self.assertEqual(poll_interval(n - 31, n, c), c.slow_interval)
        self.assertEqual(poll_interval(n - 29, n, c), c.fast_interval)
        self.assertEqual(poll_interval(n + 119, n, c), c.fast_interval)
        self.assertEqual(poll_interval(n + 121, n, c), c.slow_interval)
        self.assertEqual(poll_interval(n, None, c), c.slow_interval)


if __name__ == "__main__":
    unittest.main()
