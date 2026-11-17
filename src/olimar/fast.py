"""原生快速 adapter：常駐 Swift daemon（ScreenCaptureKit 截圖 + Vision OCR + Quartz 點擊）。

比 `orca computer` 快的原因：不必每次啟動子程序、截圖不落地、OCR 可只跑 ROI、點擊不經 Orca 的同步等待。
所有點擊 guard 在 daemon 內、送出事件前一刻執行（見 vision.swift）。Orca adapter 保留作診斷與相容。
"""
from __future__ import annotations

import json
import select
import subprocess
import time
from pathlib import Path

from . import vision
from .device import BUNDLE, DeviceError, GuardError
from .vision import Observation


class NativeDevice:
    def __init__(self, bundle: str = BUNDLE, max_age: float = 3.0, shots_dir: Path | None = None,
                 helper: Path = vision.DEFAULT_HELPER, cmd_timeout: float = 15.0):
        self.bundle, self.max_age_ms, self.helper = bundle, int(max_age * 1000), helper
        self.shots_dir, self.cmd_timeout = shots_dir, cmd_timeout
        self.p: subprocess.Popen | None = None
        self.focused = False

    # ---- daemon ----
    def start(self) -> None:
        if self.p and self.p.poll() is None:
            return
        vision.ensure_helper(self.helper)
        self.p = subprocess.Popen([str(self.helper), "serve", self.bundle], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        info = self._cmd(cmd="info")  # 暖機：載入 ScreenCaptureKit / Vision
        if not (info["ax"] and info["screen"]):
            raise DeviceError(f"缺少權限 accessibility={info['ax']} screen_recording={info['screen']}")
        self._cmd(cmd="shot", ocr="accurate", roi=[0, 0, 64, 64])  # 暖機 OCR 模型

    def _cmd(self, **req) -> dict:
        if not self.p or self.p.poll() is not None:
            raise DeviceError("native daemon 未啟動")
        self.p.stdin.write(json.dumps(req) + "\n")
        self.p.stdin.flush()
        if not select.select([self.p.stdout], [], [], self.cmd_timeout)[0]:
            self.p.kill()
            raise DeviceError(f"native daemon 逾時（{req.get('cmd')}）")
        r = json.loads(self.p.stdout.readline())
        if not r.get("ok"):
            err = r.get("error", "?")
            raise (GuardError if err.startswith("guard:") else DeviceError)(err)
        return r

    def close(self) -> None:
        if self.p and self.p.poll() is None:
            try:
                self.restore()
            finally:
                self.p.stdin.close()
                self.p.terminate()

    # ---- 焦點：run 開始時啟用一次，結束時還原；失焦則 guard 拒絕點擊，不會自動重新搶焦點 ----
    def focus(self) -> None:
        r = self._cmd(cmd="focus")
        if not r["front"]:
            raise DeviceError("無法讓 iPhone 鏡像成為最上層 app")
        self.focused = True

    def restore(self) -> None:
        if self.focused:
            self._cmd(cmd="restore")
            self.focused = False

    # ---- 觀察 ----
    def window(self, win_id: int | None = None) -> dict:
        w = self._cmd(cmd="info")["win"]
        return {"id": w["id"], "width": w["w"], "height": w["h"], "x": w["x"], "y": w["y"]}

    def observe(self, roi=None, ocr: str = "accurate") -> Observation:
        """roi＝(x,y,w,h) 或其 list，佔截圖寬高的比例；只 OCR 這些區域（單一小 ROI 約 25–40ms，全畫面約 130–200ms）。"""
        t0 = time.perf_counter()
        taken_at = time.time()
        req: dict = {"cmd": "shot", "ocr": ocr}
        if roi:
            req["rois_frac"] = [list(r) for r in (roi if isinstance(roi[0], (tuple, list)) else [roi])]
        if self.shots_dir:
            self.shots_dir.mkdir(parents=True, exist_ok=True)
            req["save"] = str(self.shots_dir / f"{int(time.time() * 1000)}.png")
        r = self._cmd(**req)
        w, h, win = r["width"], r["height"], r["win"]
        if abs(w / h - win["w"] / win["h"]) > 0.01:
            raise DeviceError(f"截圖長寬比 {w}x{h} 與視窗 {win['w']}x{win['h']} 不符")
        lines = [vision.Line(l["text"], l["conf"], l["x"], l["y"], l["w"], l["h"]) for l in r.get("lines", [])]
        sid = r["shot"]
        if r["frontmost_pid"] != win["pid"]:
            pass  # 只是資訊；點擊時 daemon 會擋
        ms = {"capture_ms": r["capture_ms"], "ocr_ms": r.get("ocr_ms", 0.0), "observe_ms": (time.perf_counter() - t0) * 1000}
        return Observation(
            lines, w, h, win["id"], win["w"], win["h"], taken_at, req.get("save", ""), shot_id=sid,
            partial=bool(roi), ms=ms,
            colors=lambda rect, s=sid: self._cmd(cmd="colors", shot=s, rect=list(rect)),
            row=lambda rect, n, s=sid: self._cmd(cmd="row", shot=s, rect=list(rect), n=n)["cells"],
            sig=lambda rect, n=16, s=sid: self._cmd(cmd="sig", shot=s, rect=list(rect), n=n)["sig"],
        )

    def save(self, obs: Observation, path: Path) -> None:
        """把該次截圖存成證據 PNG（約 100ms，不放在熱路徑上）。"""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._cmd(cmd="save", shot=obs.shot_id, path=str(path))

    # ---- 動作（guard 在 daemon 內） ----
    def _pt(self, obs: Observation, px: float, py: float) -> tuple[float, float]:
        return px * obs.win_w / obs.shot_w, py * obs.win_h / obs.shot_h

    def click(self, obs: Observation, px: float, py: float, label: str = "") -> dict:
        if obs.offline or obs.shot_id is None:
            raise GuardError("離線觀察不可點擊")
        x, y = self._pt(obs, px, py)
        return self._cmd(cmd="click", shot=obs.shot_id, x=x, y=y, max_age_ms=self.max_age_ms)

    def drag(self, obs: Observation, px: float, py: float, px2: float, py2: float) -> dict:
        if obs.offline or obs.shot_id is None:
            raise GuardError("離線觀察不可點擊")
        (x, y), (x2, y2) = self._pt(obs, px, py), self._pt(obs, px2, py2)
        return self._cmd(cmd="drag", shot=obs.shot_id, x=x, y=y, x2=x2, y2=y2, max_age_ms=self.max_age_ms)

    def scroll(self, obs: Observation, px: float, py: float, direction: str) -> dict:
        """橫向捲動＝原生拖曳（Orca 的 scroll 在實機不會移動輪播）。direction=right 表示看『右邊』的內容＝手指往左拖。"""
        d = 0.35 * obs.shot_w
        a, b = (px + d / 2, px - d / 2) if direction == "right" else (px - d / 2, px + d / 2)
        return self.drag(obs, a, py, b, py)
