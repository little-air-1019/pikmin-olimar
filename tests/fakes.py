"""Fake device：小型遊戲狀態機。點擊用真的 check_click（視窗／bounds／過舊 guard）；畫面文字為 OCR Line，
GO 鈕與篩選圓點用與真實 vision 相同的顏色統計介面（colors／row／sig）餵入。"""
import time
from pathlib import Path

from olimar import vision
from olimar.controller import BACK_FRAC, Config, Controller, GO_CENTER
from olimar.device import check_click
from olimar.store import Store
from olimar.vision import Line, Observation

W, H = 696, 1532
WIN = {"id": 1, "width": 348, "height": 766, "isMinimized": False, "isOffscreen": False}


def L(text, x, y, w=120, h=30, conf=1.0):
    return Line(text, conf, x, y, w, h)


def list_screen(remaining, cards, nexts=None):
    """cards: [(title, place, x)]；nexts: card title → 下一畫面。"""
    lines = [L("蘑菇", 51, 365, 58), L(f"今天還剩下{remaining}次", 51, 405, 173)]
    clicks = dict(nexts or {})
    for t, p, x in cards:
        lines += [L(t, x, 650, 200), L(p, x + 20, 750, 120, 20)]
    return {"lines": lines, "clicks": clicks}


def detail_screen(title, orange=0.0, names=4, cd=None, joined_to="team", leave=False):
    lines = [L(title, 438, 643, 178), L("參加", 300, 908, 54, 24), L("前往這裡", 441, 1154, 82)]
    lines += [L(f"玩家{i}", 60 + 90 * i, 1450, 60, 20) for i in range(names)]
    if cd:
        lines.append(L(f"剩下{cd // 60}分{cd % 60}秒", 34, 612, 106, 18))
    if leave:
        lines.append(L("離開", 300, 1000, 80))
    return {"lines": lines, "clicks": {"參加": joined_to, "<BACK>": "list"}, "orange": orange}


def team_screen(sel, cap, go, clicks, dots=None, grid=0, cancel=True):
    """go: disabled | plain | ticket | None；dots: {顏色: cx px}（篩選列目前可見的圓點）。"""
    lines = [L(f"選擇派出皮克敏（{sel}/{cap}）", 150, int(0.386 * H), 300), L("自動", 100, int(0.446 * H), 80)]
    if sel > 0 and cancel:
        lines.append(L("取消", 80, int(0.9 * H), 80))
    return {"lines": lines, "clicks": clicks, "go": go, "dots": dots or {}, "grid": grid}


def dialog_screen():
    return {"lines": [L("使用蘑菇券參加？", 100, 700, 300), L("確認", 100, 900), L("取消", 300, 900)], "clicks": {}}


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


STATS = {  # (GO 圓形, GO 上半) 的顏色統計；數字取自實機樣本（disabled/ticket）與保守的 plain 假設
    "ticket": ({"red": 0.106, "orange": 0.057, "white": 0.19}, {"orange": 0.176}),
    "disabled": ({"red": 0.008, "orange": 0.016, "white": 0.57}, {"orange": 0.017}),
    "plain": ({"red": 0.30, "orange": 0.01, "white": 0.20}, {"orange": 0.005}),
}


class FakeDevice:
    def __init__(self, screens, start="list", native=True):
        self.screens, self.cur, self.native = screens, start, native
        self.win = dict(WIN)
        self.log = []  # 已執行的點擊／拖曳
        self.events = []
        self.scrolls = 0
        self.observations = 0

    # ---- 生命週期 ----
    def focus(self):
        self.events.append("focus")

    def restore(self):
        self.events.append("restore")

    # ---- 觀察 ----
    def observe(self, roi=None, **_):
        s = self.screens[self.cur]
        self.observations += 1
        orange = s.get("orange")
        o = Observation(list(s["lines"]), W, H, self.win["id"], self.win["width"], self.win["height"], time.time(),
                        shot_id=self.observations, partial=bool(roi), ms={"capture_ms": 30.0, "ocr_ms": 50.0})
        rect_c, rect_t = vision._rect(o, vision.GO_CIRCLE), vision._rect(o, vision.GO_TOP)

        def colors(rect):
            if s.get("go") and tuple(rect) == rect_c:
                return dict(STATS[s["go"]][0]) | {"total": 1}
            if s.get("go") and tuple(rect) == rect_t:
                return dict(STATS[s["go"]][1]) | {"total": 1}
            return None if orange is None else {"orange": orange, "red": 0, "green": 0, "total": 1}

        def row(rect, n):
            if not self.native:
                return None
            cells = [[255, 255, 255]] * n
            x0, _, w, _ = rect
            for c, cx in s.get("dots", {}).items():
                for i in range(n):
                    if abs(x0 + (i + 0.5) * w / n - cx) < 15:
                        cells[i] = list(vision.FILTER_RGB[c])
            return cells

        def sig(rect, n=16):
            return [s.get("grid", 0)] * (n * n)

        o.colors, o.row, o.sig = colors, row, sig
        return o

    def window(self, _=None):
        return dict(self.win)

    # ---- 動作 ----
    def _hit(self, px, py):
        s = self.screens[self.cur]
        if s.get("go") and abs(px - GO_CENTER[0] * W) < 40 and abs(py - GO_CENTER[1] * H) < 40:
            return "GO"
        for c, cx in s.get("dots", {}).items():
            if abs(px - cx) < 15 and abs(py - 0.448 * H) < 20:
                return f"DOT:{c}"
        for l in s["lines"]:
            if l.x <= px <= l.x + l.w and l.y <= py <= l.y + l.h:
                return l.text
        if abs(px - W * BACK_FRAC[0]) < 40 and abs(py - H * BACK_FRAC[1]) < 40:
            return "<BACK>"
        return None

    def click(self, obs, px, py, label=""):
        check_click(obs, self.win, px, py, now=time.time(), max_age=30)
        hit = self._hit(px, py)
        self.log.append(hit)
        nxt = self.screens[self.cur]["clicks"].get(hit)
        if nxt:
            self.cur = nxt

    def drag(self, obs, px, py, px2, py2):
        check_click(obs, self.win, px, py, now=time.time(), max_age=30)
        self.log.append("<DRAG>")
        self.cur = self.screens[self.cur]["clicks"].get("<DRAG>", self.cur)

    def scroll(self, obs, px, py, direction):
        check_click(obs, self.win, px, py, now=time.time(), max_age=30)
        self.scrolls += 1
        self.log.append("<SCROLL>")
        self.cur = self.screens[self.cur]["clicks"].get("<SCROLL>", self.cur)


def make(tmp, screens, start="list", clock=None, native=True, **cfg):
    clock = clock or FakeClock()
    c = Config(runtime=Path(tmp), poll_interval=0.01, **cfg)
    store = Store(Path(tmp), "Asia/Taipei", clock=clock.now)
    dev = FakeDevice(screens, start, native)
    return Controller(dev, store, c, clock=clock, log=lambda *_: None), dev, store, clock
