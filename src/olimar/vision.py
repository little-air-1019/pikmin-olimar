"""畫面辨識：macOS Vision OCR（Swift helper）+ 規則式分類。

畫面上的所有文字都只是「資料」：只用固定 regex 解析，從不當成指令或 shell 輸入。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .domain import Mushroom

SRC = Path(__file__).with_name("vision.swift")
DEFAULT_HELPER = Path("runtime/bin/olimar-vision")


class VisionError(RuntimeError):
    pass


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).replace("参", "參")
    return re.sub(r"\s+", "", s)


@dataclass
class Line:
    text: str
    conf: float
    x: int
    y: int
    w: int
    h: int

    @property
    def n(self) -> str:
        return norm(self.text)

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


@dataclass
class Observation:
    """一次帶時間與視窗資料的觀察。座標單位：shot_* 是截圖 pixels，win_* 是視窗 points。"""
    lines: list[Line]
    shot_w: int
    shot_h: int
    win_id: int | None
    win_w: int | None
    win_h: int | None
    taken_at: float  # time.time()
    shot_path: str = ""
    offline: bool = False
    colors: Callable[[tuple[int, int, int, int]], dict | None] = field(default=lambda r: None, repr=False)
    shot_id: int | None = None  # 原生 daemon 的截圖編號（點擊 guard 用）
    partial: bool = False  # 只 OCR 了 ROI
    ms: dict = field(default_factory=dict)  # 各階段耗時（capture_ms/ocr_ms/...）
    row: Callable[..., list | None] = field(default=lambda *a: None, repr=False)  # 水平帶狀平均 RGB（僅原生）
    sig: Callable[..., list | None] = field(default=lambda *a: None, repr=False)  # 區域灰階指紋（僅原生）

    def find(self, *needles: str) -> list[Line]:
        ns = [norm(n) for n in needles]
        return [l for l in self.lines if any(n in l.n for n in ns)]

    @property
    def text(self) -> str:
        return "\n".join(l.text for l in self.lines)

    def to_dict(self) -> dict:
        return {
            "shot": [self.shot_w, self.shot_h], "window": [self.win_id, self.win_w, self.win_h],
            "taken_at": self.taken_at, "shot_path": self.shot_path, "offline": self.offline,
            "lines": [l.__dict__ for l in self.lines],
        }


# ---------- helper wrapper ----------

def ensure_helper(path: Path = DEFAULT_HELPER) -> Path:
    if sys.platform != "darwin":
        raise VisionError("需要 macOS（Vision framework）")
    if path.exists() and path.stat().st_mtime >= SRC.stat().st_mtime:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["swiftc", "-O", str(SRC), "-o", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise VisionError(f"swiftc 編譯失敗: {r.stderr.strip()[:500]}")
    return path


def _run(helper: Path, *args: str) -> dict:
    r = subprocess.run([str(helper), *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise VisionError(f"vision helper 失敗: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def ocr(png: str, helper: Path = DEFAULT_HELPER) -> tuple[int, int, list[Line]]:
    d = _run(ensure_helper(helper), "ocr", png)
    return d["width"], d["height"], [Line(l["text"], l["conf"], l["x"], l["y"], l["w"], l["h"]) for l in d["lines"]]


def row_cells(png: str, rect: tuple[int, int, int, int], n: int, helper: Path = DEFAULT_HELPER) -> list:
    return _run(ensure_helper(helper), "row", png, *map(str, rect), str(n))["cells"]


def color_stats(png: str, rect: tuple[int, int, int, int], helper: Path = DEFAULT_HELPER) -> dict:
    return _run(ensure_helper(helper), "colors", png, *map(str, rect))


# ---------- parsing ----------

def _first_int(pattern: str, lines: list[Line]) -> int | None:
    for l in lines:
        m = re.search(pattern, l.n)
        if m:
            return int(m.group(1))
    return None


def remaining_free(o: Observation) -> int | None:
    """『今天還剩下 3 次』。"""
    return _first_int(r"今天還?剩下?(\d+)次", o.lines)


def countdown_seconds(o: Observation) -> int | None:
    """『剩下 2 小時 31 分 37 秒』『預估 剩下35分14秒』；OCR 偶爾把『秒』讀成『利』，所以不強求最後一個字。"""
    for l in o.lines:
        if "剩下" not in l.n:
            continue
        m = re.search(r"剩下(?:(\d+)小時)?(?:(\d+)分)?(?:(\d+)[秒利])?", l.n)
        if m and any(m.groups()):
            h, mi, se = (int(g or 0) for g in m.groups())
            return h * 3600 + mi * 60 + se
    return None


def mushroom_of(o: Observation) -> Mushroom:
    for l in o.lines:
        if len(l.n) <= 14:
            m = re.fullmatch(r"(小型|一般|大型|巨大)(.{0,8}?)蘑菇", l.n)
            if m:
                return Mushroom(m.group(1), m.group(2) or None, l.text)
    return Mushroom(None, None, "")


def is_ticketish(o: Observation) -> bool:
    return any("券" in l.n for l in o.lines)


def is_team_view(o: Observation) -> bool:
    """隊伍選擇畫面：標題『選擇派出皮克敏（N/M）』。GO 是圖示，空隊伍時還是停用狀態，不能靠 GO 文字。"""
    return bool(o.find("選擇派出皮克敏"))


def is_list_view(o: Observation) -> bool:
    return remaining_free(o) is not None and bool(o.find("蘑菇"))


def is_detail_view(o: Observation) -> bool:
    return bool(o.find("參加")) and not is_team_view(o)


def classify(o: Observation) -> str:
    """list | detail | team | ticket_dialog | unknown。券相關文字在清單以外一律視為 ticket_dialog。"""
    is_list = remaining_free(o) is not None and bool(o.find("蘑菇"))
    if is_ticketish(o) and not is_list:
        return "ticket_dialog"
    if is_ticketish(o) and o.find("使用", "消耗", "確認", "確定"):
        return "ticket_dialog"
    if is_list:
        return "list"
    if is_team_view(o):
        return "team"
    if o.find("參加") or (o.find("大家的合計工作力") and o.find("前往這裡")):
        return "detail"
    return "unknown"


def has_leave_evidence(o: Observation) -> bool:
    """本帳號已參戰的證據：出現『離開／退出』，且畫面帶有蘑菇戰鬥脈絡（倒數或詳細頁標籤），不是隊伍頁／券視窗。
    即使 classify 因標籤 OCR 遺漏而回 unknown 也成立；單獨一個『離開』字樣（例如任意對話框）不算。"""
    if not o.find("離開", "退出") or is_team_view(o) or is_ticketish(o):
        return False
    return countdown_seconds(o) is not None or bool(o.find("大家的合計工作力", "前往這裡", "參加"))


def ticket_state(o: Observation) -> str:
    """參加鈕左側橘色像素 → ticket|free|unknown。看不出來就當有券。"""
    ls = o.find("參加")
    if not ls:
        return "unknown"
    b = max(ls, key=lambda l: l.conf)
    rect = (int(b.x - 2.4 * b.h), int(b.y - 0.6 * b.h), int(2.3 * b.h), int(2.2 * b.h))
    try:
        st = o.colors(rect)
    except Exception:
        return "unknown"
    if not st:
        return "unknown"
    if st["orange"] >= 0.03:
        return "ticket"
    if st["orange"] <= 0.002:
        return "free"
    return "unknown"


def players_seen(o: Observation) -> int:
    """詳細頁底部頭像列的名字標籤數（下限估計；漏讀時偏少）。"""
    band = [l for l in o.lines if 0.93 * o.shot_h <= l.y <= 0.97 * o.shot_h and "前往" not in l.n]
    return len(band)


@dataclass
class Card:
    title: str
    place: str
    line: Line

    @property
    def key(self) -> str:
        # 橫向捲動時卡片左側會被裁切（OCR 少讀前幾個字），所以只用尾端字元當識別
        return f"{norm(self.title)[-4:]}|{norm(self.place)[-3:]}"


def cards(o: Observation) -> list[Card]:
    """探險清單的蘑菇卡（依 x 排序）。標題＝以『蘑菇』結尾、≥4 字、無數字的行；地點取其下方最近、x 重疊的行。"""
    out = []
    for l in o.lines:
        if l.n.endswith("蘑菇") and len(l.n) >= 4 and not re.search(r"\d", l.n) and l.y > 0.3 * o.shot_h:
            below = [m for m in o.lines
                     if m is not l and l.y < m.y <= l.y + 3.5 * l.h + 60
                     and m.x < l.x + l.w and m.x + m.w > l.x - 40 and "蘑菇" not in m.n and not re.search(r"\d{3}", m.n)]
            below.sort(key=lambda m: m.y)
            out.append(Card(l.text, below[0].text if below else "", l))
    return sorted(out, key=lambda c: c.line.x)


def team_counts(o: Observation) -> tuple[int, int] | None:
    """『選擇派出皮克敏（0/40）』→ (0, 40)。只認標題行，避免與『蘑菇：1/12』混淆。"""
    for l in o.find("選擇派出皮克敏"):
        m = re.search(r"\((\d+)/(\d+)\)", l.n)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


# GO 鈕（右下圓形）的位置，以截圖寬高的比例表示（由 581×1280 與 696×1532 樣本換算，兩者比例一致）。
GO_CIRCLE = (0.714, 0.848, 0.241, 0.098)
GO_TOP = (0.766, 0.844, 0.155, 0.047)  # 圓形上半：券圖示所在


def _rect(o: Observation, f: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    return int(f[0] * o.shot_w), int(f[1] * o.shot_h), int(f[2] * o.shot_w), int(f[3] * o.shot_h)


def go_state(o: Observation) -> dict:
    """GO 鈕狀態：ticket（有券圖示）| plain（啟用且無券）| disabled | unknown。

    ticket 與 disabled 有實機樣本（selection-full-ticket-go / selection-empty）。plain 沒有實機樣本，
    只用『啟用（圓形內紅色像素夠多）且圓形上半沒有橘色券圖示』推論，門檻偏保守；其他一律 unknown＝不得 GO。
    """
    try:
        circle, top = o.colors(_rect(o, GO_CIRCLE)), o.colors(_rect(o, GO_TOP))
    except Exception:
        circle = top = None
    if not circle or not top:
        return {"state": "unknown", "why": "無法取得 GO 區域顏色"}
    d = {"orange_top": round(top["orange"], 3), "red": round(circle["red"], 3), "white": round(circle["white"], 3)}
    if circle["white"] >= 0.45 and circle["red"] < 0.03:
        return {"state": "disabled", **d}
    if top["orange"] >= 0.10 and circle["red"] >= 0.05:
        return {"state": "ticket", **d}
    if circle["red"] >= 0.05 and top["orange"] <= 0.04 and circle["white"] < 0.45:
        return {"state": "plain", **d}
    return {"state": "unknown", **d}


# ---------- 顏色篩選圓點（隊伍畫面，僅圖示、無文字） ----------
# 遊戲內固定順序：紅 黃 藍 紫 白 岩 羽翅 冰。前五個的 RGB 來自實機樣本（selection-empty）；
# 白：第 5 個（被裁切，樣本 (220,232,248)）；岩／羽翅／冰在畫面外，從未在實機看到 → 只作為推測值。
FILTER_ORDER = ["紅", "黃", "藍", "紫", "白", "岩", "羽翅", "冰"]
FILTER_RGB = {"紅": (252, 85, 87), "黃": (255, 217, 4), "藍": (53, 135, 211), "紫": (220, 57, 211), "白": (225, 233, 245),
              "岩": (150, 150, 150), "羽翅": (255, 150, 200), "冰": (150, 220, 250)}  # 岩／羽翅／冰為未驗證推測
FILTER_ROW = (0.57, 0.4455, 0.43, 0.005)  # 圓心 y≈0.448（581×1280 樣本 574/1280）；x 從『工作力▼』右側開始；取細帶避免白底稀釋
FILTER_PITCH = 0.0785  # 圓點間距 / 截圖寬


def filter_dots(o: Observation) -> list[dict]:
    """掃描篩選列，回傳完整可見的圓點 [{color, cx, cy, rgb}]。需要原生 adapter 的 row()。"""
    x0, y0, w, h = _rect(o, FILTER_ROW)
    n = w // 3
    cells = o.row((x0, y0, w, h), n)
    if not cells:
        return []
    bg = lambda c: max(abs(c[0] - 255), abs(c[1] - 255), abs(c[2] - 255)) < 14
    segs, start = [], None
    for i, c in enumerate(cells + [[255, 255, 255]]):
        if not bg(c) and start is None:
            start = i
        elif bg(c) and start is not None:
            segs.append((start, i - 1))
            start = None
    dots = []
    pitch_px = FILTER_PITCH * o.shot_w
    for a, b in segs:
        width = (b - a + 1) * 3
        if not (0.45 * pitch_px <= width <= 0.95 * pitch_px):
            continue  # 太窄（被裁切／只是外框）或太寬（多個相連）
        cx = x0 + (a + b) / 2 * 3 + 1.5
        mid = cells[(a + b) // 2]
        best = min(FILTER_RGB, key=lambda k: sum((mid[j] - FILTER_RGB[k][j]) ** 2 for j in range(3)))
        err = sum((mid[j] - FILTER_RGB[best][j]) ** 2 for j in range(3)) ** 0.5
        dots.append({"color": best if err < 45 else None, "cx": cx, "cy": y0 + h / 2, "rgb": mid, "err": round(err, 1)})
    return dots
