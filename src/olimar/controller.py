"""探險清單 → 詳細頁 → 參加 → 篩選顏色 → 自動 → GO → 以『剩餘次數減 1』確認。每步重新截圖、驗證預期畫面、才點擊。

熱路徑：無 LLM、只跑 ROI OCR、以『畫面就緒輪詢』取代固定 sleep，每個階段都記錄耗時。
未知一律停止並保留證據；GO 前必須看到『GO 鈕啟用且沒有券圖示』；GO 只點一次，結果模糊就保持 pending。
"""
from __future__ import annotations

import json
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import vision
from .device import BUNDLE, DeviceError, GuardError
from .domain import Decision, decide_detail
from .scheduler import Clock, Stopper, next_candidate_time, poll_interval
from .store import LockBusy, PendingBlocks, Store
from .vision import Line, Observation

# 詳細頁／隊伍頁左下角『‹』返回鈕（圖示、無文字），用比例座標；點後必須驗證畫面。
BACK_FRAC = (0.115, 0.911)
# ROI（佔截圖寬高比例）：熱路徑只 OCR 這些區域。
# 多個細長 ROI 比一個大 ROI 便宜（OCR 成本隨面積增加）。
ROI_LIST = [(0.0, 0.23, 1.0, 0.055), (0.0, 0.40, 1.0, 0.12)]  # 蘑菇／今天還剩下 N 次；卡片標題與地點
ROI_DETAIL = [(0.0, 0.47, 0.7, 0.08), (0.0, 0.54, 1.0, 0.11), (0.0, 0.68, 1.0, 0.08)]  # 倒數（.49–.53）；標題；參加
ROI_TEAM = (0.0, 0.37, 1.0, 0.10)  # 選擇派出皮克敏（N/M）+ 篩選列左側『自動』
GRID = (0.03, 0.49, 0.94, 0.42)  # 皮克敏格，用來偵測篩選是否生效
GO_CENTER = (vision.GO_CIRCLE[0] + vision.GO_CIRCLE[2] / 2, vision.GO_CIRCLE[1] + vision.GO_CIRCLE[3] / 2)


@dataclass
class Config:
    bundle: str = BUNDLE
    device: str = "orca"  # orca | native（快速）
    timezone: str = "Asia/Taipei"
    runtime: Path = Path("runtime")
    respawn_seconds: int = 300
    max_obs_age: float = 3.0  # 觀察超過幾秒就拒絕點擊
    max_cards: int = 3
    max_scrolls: int = 3
    poll_interval: float = 0.04  # 就緒輪詢間隔
    wait_timeout: float = 3.0  # 每個畫面轉換最多等多久
    confirm_timeout: float = 8.0  # GO 後等待『剩餘次數減 1』的上限
    require_full_team: bool = False
    color_mode: str = "verify"  # verify：依蘑菇種類篩選顏色並驗證；auto：一律交給遊戲「自動」
    filter_swipes: int = 3
    scroll_settle: float = 4.0  # 拖曳後最多等多久讓輪播停穩（實測慣性約 2s）
    slow_interval: float = 30.0  # watch：離預估重生時間很遠時
    fast_interval: float = 0.5  # watch：預估重生時間附近
    near_before: float = 30.0
    near_after: float = 120.0

    def __post_init__(self):
        if self.color_mode not in ("verify", "auto"):
            raise ValueError(f"color_mode 只能是 verify 或 auto，收到 {self.color_mode!r}")
        if self.device not in ("orca", "native"):
            raise ValueError(f"device 只能是 orca 或 native，收到 {self.device!r}")


@dataclass
class Result:
    status: str  # dispatched | pending | stopped | skipped | no_candidate | planned | smoke_ok
    detail: str
    steps: list[str] = field(default_factory=list)
    next_at: float | None = None
    timings: dict = field(default_factory=dict)

    def to_dict(self):
        return self.__dict__


class Stop(Exception):
    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


class WaitTimeout(Stop):
    """只有『逾時』可以重試返回；券警告等其他 Stop 必須直接結束。"""


class Controller:
    def __init__(self, device, store: Store, cfg: Config, clock=None, log=print):
        self.dev, self.store, self.cfg = device, store, cfg
        self.clock, self.log = clock or Clock(), log
        self.steps: list[str] = []
        self.tm: dict = {}
        self.t0 = 0.0

    # ---------- helpers ----------
    def _say(self, msg: str) -> None:
        self.steps.append(msg)
        self.log(msg)

    def _mark(self, stage: str) -> None:
        self.tm[stage] = round((time.perf_counter() - self.t0) * 1000, 1)

    def _acc(self, o: Observation) -> None:
        for k in ("capture_ms", "ocr_ms"):
            self.tm[k] = round(self.tm.get(k, 0) + o.ms.get(k, 0), 1)
        self.tm["observes"] = self.tm.get("observes", 0) + 1

    def _ticketish(self, o: Observation) -> bool:
        return vision.is_ticketish(o) and not (vision.is_list_view(o) and not o.find("使用", "消耗", "確認", "確定"))

    def _observe(self, expect: str | tuple[str, ...] | None = None) -> Observation:
        """全畫面觀察＋完整分類（起始、GO 前 guard、GO 後驗證用）。"""
        o = self.dev.observe()
        self._acc(o)
        scr = vision.classify(o)
        self._say(f"觀察(全) → {scr}")
        if scr == "ticket_dialog":
            raise Stop("stopped", "偵測到蘑菇券相關視窗：不點擊任何東西（含取消），請人工處理")
        if expect and scr not in ((expect,) if isinstance(expect, str) else expect):
            raise Stop("stopped", f"預期畫面 {expect}，實際 {scr}")
        return o

    def _wait(self, name: str, pred, roi, timeout: float | None = None, stable: bool = False) -> Observation:
        """輪詢直到 pred(obs) 為真（畫面就緒）。stable=True 還要求連續兩次 OCR 位置相同（轉場動畫結束，避免點擊被吃掉）。
        券文字一出現立即停止；逾時停止，絕不在未確認的畫面上點擊。"""
        deadline = self.clock.now() + (timeout or self.cfg.wait_timeout)
        prev = None
        while True:
            o = self.dev.observe(roi=roi)
            self._acc(o)
            if self._ticketish(o):
                raise Stop("stopped", f"等待 {name} 時偵測到蘑菇券相關視窗：不點擊任何東西")
            if pred(o):
                sig = tuple((l.n, l.y // 3) for l in o.lines if not any(c.isdigit() for c in l.n))  # 略過會跳動的數字（時間、倒數）
                if not stable or sig == prev:
                    self._mark(name)
                    return o
                prev = sig
            else:
                prev = None
            if self.clock.now() >= deadline:
                raise WaitTimeout("stopped", f"等待畫面 {name} 逾時（{timeout or self.cfg.wait_timeout}s）")
            self.clock.sleep(self.cfg.poll_interval)

    def _click(self, obs: Observation, x: float, y: float, label: str) -> None:
        t = time.perf_counter()
        try:
            self.dev.click(obs, x, y, label)
        except GuardError as e:
            raise Stop("stopped", f"點擊被 guard 拒絕：{e}")
        self.tm["click_ms"] = round(self.tm.get("click_ms", 0) + (time.perf_counter() - t) * 1000, 1)
        self._say(f"點擊 {label} @px({x:.0f},{y:.0f})")

    def _click_line(self, obs: Observation, l: Line, label: str) -> None:
        self._click(obs, *l.center, label)

    def _best(self, obs: Observation, *needles: str) -> Line | None:
        ls = obs.find(*needles)
        return max(ls, key=lambda l: l.conf) if ls else None

    def _back(self, obs: Observation, expect_pred, roi) -> Observation:
        """返回。點擊偶爾會被轉場動畫吃掉：逾時後若仍停在詳細頁，才重點一次（絕不在清單上重點：那裡同位置是關閉鈕）。"""
        for attempt in range(2):
            self._click(obs, obs.shot_w * BACK_FRAC[0], obs.shot_h * BACK_FRAC[1], "返回")
            try:
                return self._wait("back", expect_pred, roi, timeout=1.5)
            except WaitTimeout:
                if attempt == 1:
                    raise
                o = self.dev.observe()  # 全畫面：券警告可能落在 ROI 之外
                self._acc(o)
                if self._ticketish(o) or vision.classify(o) == "ticket_dialog":
                    raise Stop("stopped", "返回逾時後偵測到蘑菇券相關視窗：不再點擊")
                if not vision.is_detail_view(o):
                    raise
                obs = o
        raise Stop("stopped", "返回失敗")

    def _to_list(self) -> Observation:
        """從任何已知畫面回到清單（唯一會碰『取消』的地方，僅 smoke／收尾）。"""
        cancelled = False
        for _ in range(10):
            o = self._observe()
            scr = vision.classify(o)
            if scr == "list":
                return o
            if scr == "unknown":  # 轉場動畫中：等一下再看，不點擊
                self.clock.sleep(0.4)
                continue
            if scr == "team":
                c = self._best(o, "取消")
                tc = vision.team_counts(o)
                if c and not cancelled and tc and tc[0] > 0:  # 取消只是取消選取，且只按一次；之後一律用返回鈕
                    self._click_line(o, c, "取消（取消選取）")
                    cancelled = True
                else:
                    self._click(o, o.shot_w * BACK_FRAC[0], o.shot_h * BACK_FRAC[1], "返回")
            elif scr == "detail":
                self._click(o, o.shot_w * BACK_FRAC[0], o.shot_h * BACK_FRAC[1], "返回")
            else:
                raise Stop("stopped", f"無法從 {scr} 回到清單")
            self.clock.sleep(self.cfg.poll_interval * 5)
        raise Stop("stopped", "回清單失敗")

    # ---------- plan（唯讀）----------
    def plan(self, obs: Observation) -> Result:
        scr = vision.classify(obs)
        s = [f"畫面：{scr}"]
        if scr == "ticket_dialog":
            s.append("停止：蘑菇券相關視窗，不點擊任何東西")
            return Result("stopped", s[-1], s)
        if scr == "list":
            rem = vision.remaining_free(obs)
            cs = vision.cards(obs)
            s.append(f"今日剩餘免費次數：{rem}；可見蘑菇卡 {len(cs)} 張：" + "、".join(f"{c.title.strip()}@{c.place.strip()}" for c in cs))
            if not rem:
                s.append("停止：免費次數為 0 或無法讀取")
                return Result("stopped", s[-1], s)
            if not cs:
                s.append("無可見蘑菇卡；會嘗試橫向捲動（最多 %d 次）" % self.cfg.max_scrolls)
                return Result("planned", s[-1], s)
            s.append(f"將依序開啟最多 {self.cfg.max_cards} 張卡（由左至右），逐張在詳細頁重新判斷；首張：{cs[0].title.strip()}")
            return Result("planned", "將開啟首張候選並在詳細頁判斷", s)
        if scr == "detail":
            m = vision.mushroom_of(obs)
            ticket = vision.ticket_state(obs)
            seen = vision.players_seen(obs)
            s.append(f"蘑菇：大小={m.size} 種類={m.kind} 券圖示={ticket} 可見玩家名≥{seen}")
            d = decide_detail(m, remaining=1, ticket=ticket, players_seen=seen)  # 詳細頁不顯示次數：假設 ≥1 預演
            s.append(f"決策(假設剩餘≥1)：{d.action}｜{d.why()}")
            if d.action == "join":
                j = self._best(obs, "參加")
                s.append(f"點擊『參加』@px{j.center}；之後{'篩選顏色 ' + m.color if m.color and self.cfg.color_mode == 'verify' else '不篩色'}→自動→"
                         "全畫面重新確認隊伍量/GO 無券圖示→GO 一次→以剩餘次數減 1 確認")
                return Result("planned", "可加入（實際執行前仍會回清單確認剩餘次數並重新觀察）", s)
            return Result("skipped", d.why(), s)
        if scr == "team":
            tc = vision.team_counts(obs)
            g = vision.go_state(obs)
            s.append(f"隊伍量：{tc}；GO 狀態：{g}")
            if g["state"] != "plain":
                s.append(f"停止：GO 鈕狀態 {g['state']}（只有 plain＝啟用且無券圖示才會 GO）")
                return Result("stopped", s[-1], s)
            return Result("planned", "隊伍畫面：run 會驗證最小隊伍與 GO 無券後才 GO", s)
        s.append("未知畫面：停止")
        return Result("stopped", "未知畫面", s)

    # ---------- run ----------
    def run_once(self, live: bool, go: bool = True) -> Result:
        """live=False：只預演。go=False（smoke）：走完導覽與選隊，但在 GO 前收手並取消，絕不點 GO。"""
        self.steps, self.tm = [], {}
        self.t0 = time.perf_counter()
        if not live:
            return self.plan(self.dev.observe())
        try:
            with self.store.run_lock():
                self.dev.focus()
                try:
                    return self._run_live(go)
                finally:
                    self.dev.restore()
        except LockBusy as e:
            return Result("stopped", str(e), self.steps)
        except Stop as e:
            return Result(e.status, e.detail, self.steps, timings=self.tm)
        except (DeviceError, vision.VisionError) as e:
            return Result("stopped", f"裝置／辨識錯誤：{e}", self.steps, timings=self.tm)

    def _run_live(self, go: bool) -> Result:
        if self.store.pending():
            raise Stop("stopped", "存在 pending 派遣（結果不明），禁止再次 GO；請 `olimar reconcile`")
        obs = self._observe(("list", "detail"))
        if vision.classify(obs) == "detail":
            self._say("起始於詳細頁：先返回清單以讀取實際剩餘次數")
            obs = self._back(obs, vision.is_list_view, ROI_LIST)
        self._mark("list_ready")
        visited: set[str] = set()
        opened = scrolls = 0
        while True:
            rem = vision.remaining_free(obs)
            if rem is None:
                raise Stop("stopped", "無法讀取今日剩餘免費次數")
            self.store.record_display(rem)
            if rem <= 0:
                raise Stop("stopped", "今日免費次數為 0")
            fresh = [c for c in vision.cards(obs) if c.key not in visited and 0 < c.line.center[0] < obs.shot_w]
            if not fresh or opened >= self.cfg.max_cards:
                if opened >= self.cfg.max_cards or scrolls >= self.cfg.max_scrolls:
                    return Result("no_candidate", f"已檢查 {opened} 張卡，無可免費加入者", self.steps, timings=self.tm)
                before = self._card_pos(obs)
                y = vision.cards(obs)[0].line.center[1] if vision.cards(obs) else obs.shot_h * 0.45
                try:
                    self._say("橫向捲動清單")
                    self.dev.scroll(obs, obs.shot_w / 2, y, "right")
                except GuardError as e:
                    raise Stop("stopped", f"捲動被 guard 拒絕：{e}")
                scrolls += 1
                obs, moved = self._wait_carousel(before)
                if not moved:
                    return Result("no_candidate", f"清單未變動（到盡頭或捲動無效，已等 {self.cfg.scroll_settle}s）；已檢查 {opened} 張卡", self.steps, timings=self.tm)
                continue
            card = fresh[0]
            visited.add(card.key)
            opened += 1
            self._click_line(obs, card.line, f"卡片 {card.title.strip()}")
            obs = self._wait("detail", vision.is_detail_view, ROI_DETAIL, stable=True)
            m, ticket, seen = vision.mushroom_of(obs), vision.ticket_state(obs), vision.players_seen(obs)
            cd = vision.countdown_seconds(obs)
            detail_end = self.clock.now() + cd if cd else None  # 觀察當下換成絕對時間，之後不再加上流逝秒數
            if detail_end:  # 連沒搶的滿員蘑菇也記下預計結束時間，讓 watch 提早進入快速輪詢
                self.store.record_sighting(detail_end, f"{m.size} {m.kind}")
            dec = decide_detail(m, remaining=rem, ticket=ticket, players_seen=seen)
            self._say(f"決策 {dec.action}：{dec.why()}")
            if dec.action == "stop":
                raise Stop("stopped", dec.why())
            if dec.action == "skip":
                obs = self._back(obs, vision.is_list_view, ROI_LIST)
                continue
            return self._join_and_dispatch(obs, dec, rem, go, detail_end)

    @staticmethod
    def _card_pos(o: Observation) -> dict[str, int]:
        """卡片 key → 標題中心 x（截圖像素）。"""
        return {c.key: c.line.center[0] for c in vision.cards(o)}

    def _wait_carousel(self, before: dict[str, int]) -> tuple[Observation, bool]:
        """拖曳後有界等待輪播『有進展且穩定』。進展＝出現新卡，或同樣的卡水平位移超過 3% 寬；穩定＝連兩次輪詢位置一致（≤1% 寬）。
        實測（原生拖曳）：輪播有慣性，翻到下一張後約 2 秒才停穩，中途每次輪詢的位置與 key 都在變。所以：
        - 從未出現進展 → 真正的盡頭（回 False）；OCR 抖動不算進展。
        - 有進展但直到逾時都沒停穩 → 不能在移動中的卡片上點擊，也不能誤判盡頭 → Stop。"""
        deadline = self.clock.now() + self.cfg.scroll_settle
        last, o, ever = None, None, False
        while True:
            o = self.dev.observe(roi=ROI_LIST)
            self._acc(o)
            if self._ticketish(o):
                raise Stop("stopped", "捲動後偵測到蘑菇券相關視窗：不點擊任何東西")
            cur = self._card_pos(o) if vision.is_list_view(o) else None
            if cur is not None:
                tol = 0.03 * o.shot_w
                moved = set(cur) != set(before) or any(abs(cur[k] - before[k]) > tol for k in cur)
                ever = ever or moved
                steady = last is not None and set(cur) == set(last) and all(abs(cur[k] - last[k]) <= 0.01 * o.shot_w for k in cur)
                if moved and steady:
                    self._mark("list")
                    return o, True
            last = cur
            if self.clock.now() >= deadline:
                if ever:
                    raise Stop("stopped", f"輪播在 {self.cfg.scroll_settle}s 內沒有停穩：不在移動中的卡片上點擊")
                return o, False
            self.clock.sleep(self.cfg.poll_interval)

    # ---------- 選色 ----------
    def _select_color(self, obs: Observation, color: str) -> Observation:
        if obs.row(vision._rect(obs, vision.FILTER_ROW), 8) is None:
            raise Stop("stopped", "顏色篩選圓點只有圖示：需要 --device native（原生 adapter）才能辨識；已加入但未 GO")
        y = 0.448 * obs.shot_h
        for swipe in range(self.cfg.filter_swipes + 1):
            dots = vision.filter_dots(obs)
            tgt = [d for d in dots if d["color"] == color]
            self._say(f"篩選列可見圓點：{[d['color'] for d in dots]}")
            if tgt:
                break
            if swipe == self.cfg.filter_swipes:
                raise Stop("stopped", f"找不到顏色『{color}』篩選圓點（已滑動 {swipe} 次）；已加入但未 GO")
            self._say("滑動篩選列")
            try:
                self.dev.drag(obs, 0.9 * obs.shot_w, y, 0.9 * obs.shot_w - 2.2 * vision.FILTER_PITCH * obs.shot_w, y)
            except GuardError as e:
                raise Stop("stopped", f"滑動被 guard 拒絕：{e}")
            self.clock.sleep(0.3)
            self._wait("filter_row", vision.is_team_view, ROI_TEAM)
            obs = self._observe("team")  # 動作前用全畫面確認沒有券視窗
        before = obs.sig(vision._rect(obs, GRID), 16)
        if before is None:
            raise Stop("stopped", "無法取得皮克敏格指紋，無法驗證篩選")
        d = tgt[0]
        self._click(obs, d["cx"], d["cy"], f"篩選 {color}")

        def changed(o: Observation) -> bool:
            after = o.sig(vision._rect(o, GRID), 16)
            return bool(after) and sum(abs(a - b) for a, b in zip(after, before)) / (255 * len(before)) > 0.03

        try:
            self._wait("filter_applied", lambda o: vision.is_team_view(o) and changed(o), ROI_TEAM)
            return self._observe("team")  # 下一個動作（自動）之前全畫面確認
        except Stop as e:
            raise Stop("stopped", f"點擊『{color}』篩選後皮克敏格沒有變化，無法驗證篩選生效：{e.detail}")

    # ---------- 加入 → 隊伍 → GO ----------
    def _join_and_dispatch(self, obs: Observation, dec: Decision, rem: int, go: bool, detail_end: float | None = None) -> Result:
        m = dec.mushroom
        # ROI 只用來判斷『就緒』；每次會改變遊戲狀態的點擊之前，都用全畫面重新確認畫面與券圖示。
        obs = self._observe("detail")
        if vision.ticket_state(obs) != "free":
            raise Stop("stopped", "參加前全畫面複查：參加鈕旁有券圖示或無法判斷，不點擊")
        self._click_line(obs, self._best(obs, "參加"), "參加")
        self._wait("team", vision.is_team_view, ROI_TEAM, stable=True)  # 券視窗／未知畫面 → Stop，不再點擊
        obs = self._observe("team")
        if m.color and self.cfg.color_mode == "verify":
            obs = self._select_color(obs, m.color)
        auto = self._best(obs, "自動")  # obs 此刻必為全畫面觀察（見上）
        if not auto:
            raise Stop("stopped", "找不到『自動』鈕")
        self._click_line(obs, auto, "自動")

        def filled(o: Observation) -> bool:
            tc = vision.team_counts(o)
            return bool(tc) and tc[0] >= m.min_team

        try:
            self._wait("auto", filled, ROI_TEAM)
        except Stop as e:
            raise Stop("stopped", f"自動選隊後隊伍量未達 {m.size}蘑菇最小 {m.min_team}：{e.detail}")
        # GO 前 guard：全畫面重新取得（不沿用任何先前資料）
        obs = self._observe("team")
        tc = vision.team_counts(obs)
        if tc is None or tc[0] < m.min_team:
            raise Stop("stopped", f"隊伍量 {tc} 未達最小 {m.min_team}，不派遣")
        if self.cfg.require_full_team and tc[0] < tc[1]:
            raise Stop("stopped", f"隊伍 {tc[0]}/{tc[1]} 未滿（require_full_team）")
        g = vision.go_state(obs)
        self._say(f"GO 狀態 {g}")
        self._mark("guard_ready")
        if g["state"] != "plain" and hasattr(self.dev, "save"):  # 拒絕 GO 時留下影像，供校準 plain 門檻
            try:
                self.dev.save(obs, self.cfg.runtime / "evidence" / f"go-refused-{g['state']}-{int(time.time() * 1000)}.png")
            except Exception:
                pass
        if g["state"] == "ticket":
            raise Stop("stopped", "GO 鈕出現蘑菇券圖示（名額已被搶走／超過免費額度）：不派遣、不使用蘑菇券")
        if g["state"] != "plain":
            raise Stop("stopped", f"GO 鈕狀態為 {g['state']}，無法證明免費可派遣：不 GO（{g}）")
        ev = self._save_evidence(obs, "pre-go")
        if not go:  # smoke：到此為止，取消並回清單
            self._to_list()
            return Result("smoke_ok", f"smoke：{m.size} {m.kind} 走到 GO 前 guard（GO={g['state']}，隊伍 {tc}），已取消並回清單，未 GO",
                          self.steps, timings=self.tm)
        team_cd = vision.countdown_seconds(obs)  # 隊伍頁上方也有倒數（較新）；讀不到就用詳細頁觀察當下換算的絕對時間
        end0 = self.clock.now() + team_cd if team_cd else detail_end
        # write-ahead：先落 pending，再點 GO（僅此一次）
        try:
            did = self.store.begin_dispatch(f"{m.size} {m.kind}", m.color, ev)
        except PendingBlocks as e:
            raise Stop("stopped", str(e))
        try:
            self._click(obs, GO_CENTER[0] * obs.shot_w, GO_CENTER[1] * obs.shot_h, "GO")
        except Stop as e:  # guard 在真正點擊前拒絕 → 確定沒送出
            self.store.finish(did, "failed", f"GO 前 guard 拒絕：{e.detail}")
            raise
        except Exception as e:  # 中途失敗：不知道有沒有點到 → 保持 pending
            self.store.finish(did, "pending", f"GO 點擊結果不明：{e}")
            return Result("pending", f"GO 點擊結果不明：{e}（禁止重試）", self.steps, timings=self.tm)
        self._mark("go_clicked")
        if hasattr(self.dev, "save"):  # GO 之後才存 PNG 證據（不佔用 GO 前的熱路徑）
            try:
                self.dev.save(obs, self.cfg.runtime / "evidence" / f"go-{did}-pre.png")
            except Exception:
                pass
        if "list_ready" in self.tm:
            self.tm["discover_to_go_ms"] = round(self.tm["go_clicked"] - self.tm["list_ready"], 1)
        return self._verify_go(did, m, rem, end0)

    def _verify_go(self, did: int, m, before: int, end: float | None) -> Result:
        """派遣證據＝清單『今天還剩下 N 次』比 GO 前減 1（或詳細頁出現離開／退出）。單獨的倒數不算：那可能是別人先開的戰鬥。"""
        deadline = self.clock.now() + self.cfg.confirm_timeout
        last = "GO 後尚未看到證據"
        while self.clock.now() < deadline:
            try:
                o = self.dev.observe()
            except Exception as e:
                self.store.finish(did, "pending", f"GO 後觀察失敗：{e}", end)
                return Result("pending", f"GO 後觀察失敗：{e}", self.steps, timings=self.tm)
            self._acc(o)
            scr = vision.classify(o)
            self._say(f"GO 後觀察 → {scr}")
            fresh = vision.countdown_seconds(o)
            if fresh:
                end = self.clock.now() + fresh
            if scr == "ticket_dialog":
                self.store.finish(did, "failed", "GO 後出現券視窗（競態）；未點擊任何東西")
                return Result("stopped", "GO 後出現蘑菇券視窗：未確認、未點擊，請人工處理", self.steps, timings=self.tm)
            if scr == "list":
                rem = vision.remaining_free(o)
                if rem is not None and rem <= before - 1:
                    self.store.finish(did, "confirmed", f"剩餘次數 {before}→{rem}", end)
                    self._mark("confirmed")
                    return Result("dispatched", f"已派遣 {m.size} {m.kind}：剩餘免費次數 {before}→{rem}", self.steps,
                                  next_candidate_time(end, self.cfg.respawn_seconds), self.tm)
                last = f"清單剩餘次數 {rem}（GO 前 {before}）尚未減少"
            elif vision.has_leave_evidence(o):  # 不依賴 classify：標籤 OCR 漏字也認得
                self.store.finish(did, "confirmed", "畫面出現離開／退出鈕（本帳號已參戰）", end)
                self._mark("confirmed")
                return Result("dispatched", f"已派遣 {m.size} {m.kind}：畫面出現離開／退出", self.steps,
                              next_candidate_time(end, self.cfg.respawn_seconds), self.tm)
            elif scr == "detail":
                try:  # 回清單讀次數
                    self._click(o, o.shot_w * BACK_FRAC[0], o.shot_h * BACK_FRAC[1], "返回(讀取剩餘次數)")
                except Stop:
                    pass
                last = "GO 後在詳細頁，無離開／退出證據"
            elif scr == "team":
                last = "GO 後仍在隊伍畫面"
            else:
                last = f"GO 後畫面 {scr}"
            self.clock.sleep(0.25)
        self.store.finish(did, "pending", last, end)
        return Result("pending", f"GO 後 {self.cfg.confirm_timeout}s 內無法證明本帳號已派遣（{last}）：保持 pending，不重試，需人工 reconcile",
                      self.steps, timings=self.tm)

    def _save_evidence(self, o: Observation, tag: str) -> str:
        d = self.cfg.runtime / "evidence"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{tag}-{int(time.time() * 1000)}.json"
        p.write_text(json.dumps(o.to_dict(), ensure_ascii=False, indent=1))
        return str(p)

    # ---------- watch ----------
    def watch(self, live: bool, timeout: float, stop_file: Path, max_dispatches: int = 1) -> list[Result]:
        """有界監控。離預估重生時間很遠時慢速輪詢；接近（−near_before…+near_after 秒）時快速輪詢。預估只是提示，每次都重新讀取。"""
        if max_dispatches < 1:
            raise ValueError("max_dispatches 必須 >= 1")
        stopper = Stopper(self.clock, timeout, stop_file)
        results, done = [], 0
        old = stopper.install()
        try:
            self._watch_loop(live, stopper, results, done, max_dispatches)
        finally:
            signal.signal(signal.SIGINT, old)
        return results

    def _watch_loop(self, live, stopper, results, done, max_dispatches) -> None:
        while not (why := stopper.reason()):
            r = self.run_once(live)
            results.append(r)
            self.log(f"[watch] {r.status}: {r.detail}")
            if r.status == "dispatched":
                done += 1
                if done >= max_dispatches:
                    break
            elif r.status in ("pending", "stopped") or not live:
                break  # 需要人工／預演只跑一輪
            now = self.clock.now()
            ends = self.store.upcoming_ends(now - self.cfg.respawn_seconds - self.cfg.near_after)
            last = self.store.last_expected_end()
            times = [e + self.cfg.respawn_seconds for e in ends] + ([last + self.cfg.respawn_seconds] if last else [])
            stopper.sleep(poll_interval(now, times, self.cfg))
        else:
            self.log(f"[watch] 結束：{why}")
