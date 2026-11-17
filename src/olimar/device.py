"""封裝 `orca computer`：截圖、points/pixels 轉換、點擊前驗證。只操作指定 bundle 的單一視窗。"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from . import vision
from .vision import Observation

BUNDLE = "com.apple.ScreenContinuity"


class DeviceError(RuntimeError):
    pass


class GuardError(DeviceError):
    """點擊前驗證失敗：視窗失效／尺寸變動／觀察過舊／越界。"""


def px_to_pt(obs: Observation, px: float, py: float) -> tuple[float, float]:
    return px * obs.win_w / obs.shot_w, py * obs.win_h / obs.shot_h


def check_click(obs: Observation, cur_win: dict, px: float, py: float, *, now: float, max_age: float) -> tuple[float, float]:
    """回傳視窗內 points 座標；任何不確定都 raise GuardError。純函式，方便測試。"""
    if obs.offline or obs.win_id is None:
        raise GuardError("離線觀察不可點擊")
    if now - obs.taken_at > max_age:
        raise GuardError(f"觀察已過舊 {now - obs.taken_at:.1f}s > {max_age}s")
    if cur_win.get("id") != obs.win_id:
        raise GuardError(f"視窗 id 改變 {obs.win_id} → {cur_win.get('id')}")
    if (cur_win.get("width"), cur_win.get("height")) != (obs.win_w, obs.win_h):
        raise GuardError(f"視窗尺寸改變 {obs.win_w}x{obs.win_h} → {cur_win.get('width')}x{cur_win.get('height')}")
    if cur_win.get("isMinimized") or cur_win.get("isOffscreen"):
        raise GuardError("視窗最小化或在螢幕外")
    x, y = px_to_pt(obs, px, py)
    if not (0 < x < obs.win_w and 0 < y < obs.win_h):
        raise GuardError(f"點擊超出視窗 bounds ({x:.1f},{y:.1f}) vs {obs.win_w}x{obs.win_h}")
    return x, y


class OrcaDevice:
    def __init__(self, bundle: str = BUNDLE, max_age: float = 10.0, shots_dir: Path | None = None,
                 helper: Path = vision.DEFAULT_HELPER):
        self.bundle, self.max_age, self.helper = bundle, max_age, helper
        self.shots_dir = shots_dir

    def _orca(self, *args: str) -> dict:
        r = subprocess.run(["orca", "computer", *args, "--json"], capture_output=True, text=True, timeout=60)
        try:
            d = json.loads(r.stdout)
        except json.JSONDecodeError:
            raise DeviceError(f"orca 輸出非 JSON (rc={r.returncode}): {r.stdout[:200]!r} {r.stderr[:200]!r}")
        if r.returncode != 0 or not d.get("ok", False):
            raise DeviceError(f"orca 失敗: {json.dumps(d, ensure_ascii=False)[:300]}")
        return d["result"]

    def _state(self, *extra: str) -> dict:
        return self._orca("get-app-state", "--app", self.bundle, *extra)

    def window(self, win_id: int | None = None) -> dict:
        extra = ["--window-id", str(win_id)] if win_id is not None else []
        w = self._state("--no-screenshot", *extra)["snapshot"]["window"]
        return w

    def observe(self, roi=None, **_) -> Observation:
        """roi 只有原生 adapter 用得到；Orca 一律全畫面。"""
        t0 = time.perf_counter()
        taken_at = time.time()  # 截圖之前就記錄：OCR／複製檔案的耗時也算進『觀察年齡』
        res = self._state()
        shot, w = res["screenshot"], res["snapshot"]["window"]
        # 同一視窗的截圖尺寸會變（696×1532／581×1280／494×1088），所以逐次逐軸計算比例，只檢查長寬比。
        if abs(shot["width"] / shot["height"] - w["width"] / w["height"]) > 0.01:
            raise DeviceError(f"截圖長寬比 {shot['width']}x{shot['height']} 與視窗 {w['width']}x{w['height']} 不符")
        sw, sh, lines = vision.ocr(shot["path"], self.helper)
        if (sw, sh) != (shot["width"], shot["height"]):
            raise DeviceError(f"截圖檔尺寸 {sw}x{sh} 與 orca 回報 {shot['width']}x{shot['height']} 不符")
        path = shot["path"]
        if self.shots_dir:  # 保存證據（orca 的暫存截圖會過期）
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            dst = self.shots_dir / f"{int(time.time() * 1000)}.png"
            dst.write_bytes(Path(path).read_bytes())
            path = str(dst)
        return Observation(lines, sw, sh, w["id"], w["width"], w["height"], taken_at, path,
                           colors=lambda r, p=path, h=self.helper: vision.color_stats(p, r, h),
                           ms={"observe_ms": (time.perf_counter() - t0) * 1000})

    def click(self, obs: Observation, px: float, py: float, label: str = "") -> tuple[float, float]:
        cur = self.window(obs.win_id)
        x, y = check_click(obs, cur, px, py, now=time.time(), max_age=self.max_age)
        self._orca("click", "--app", self.bundle, "--window-id", str(obs.win_id),
                   "--x", str(round(x)), "--y", str(round(y)), "--no-screenshot")
        return x, y

    def focus(self) -> None:  # Orca 自行處理焦點
        pass

    def restore(self) -> None:
        pass

    def close(self) -> None:
        pass

    def scroll(self, obs: Observation, px: float, py: float, direction: str) -> None:
        cur = self.window(obs.win_id)
        x, y = check_click(obs, cur, px, py, now=time.time(), max_age=self.max_age)
        self._orca("scroll", "--app", self.bundle, "--window-id", str(obs.win_id),
                   "--x", str(round(x)), "--y", str(round(y)), "--direction", direction, "--pages", "1",
                   "--no-screenshot")


def offline_observation(png: str, helper: Path = vision.DEFAULT_HELPER) -> Observation:
    """本地截圖 → Observation。沒有視窗資訊，因此永遠不可點擊。"""
    sw, sh, lines = vision.ocr(png, helper)
    return Observation(lines, sw, sh, None, None, None, time.time(), png, offline=True,
                       colors=lambda r: vision.color_stats(png, r, helper),
                       row=lambda r, n: vision.row_cells(png, r, n, helper))
