"""olimar CLI。read-only 命令（doctor/observe/plan/status）從不點擊；只有 run/watch 加 --live 才會操作遊戲。"""
from __future__ import annotations

import argparse
import dataclasses
import json
import atexit
import contextlib
import shutil
import statistics
import sys
import time
import tomllib
from pathlib import Path

from . import vision
from .controller import Config, Controller
from .device import OrcaDevice, offline_observation
from .fast import NativeDevice
from .store import LockBusy, Store, day_of


def load_config(path: str | None) -> Config:
    cfg = Config()
    if path:
        raw = tomllib.loads(Path(path).read_text())
        fields = {f.name for f in dataclasses.fields(Config)}
        unknown = set(raw) - fields
        if unknown:
            raise SystemExit(f"設定檔含未知欄位：{sorted(unknown)}")
        for k, v in raw.items():
            setattr(cfg, k, Path(v) if k == "runtime" else v)
        cfg.__post_init__()
    return cfg


def _dev(cfg: Config, save: bool = False):
    helper = cfg.runtime / "bin" / "olimar-vision"
    shots = cfg.runtime / "shots" if save else None
    if cfg.device == "native":
        d = NativeDevice(cfg.bundle, cfg.max_obs_age, shots, helper)
        d.start()
        atexit.register(d.close)
        return d
    return OrcaDevice(cfg.bundle, cfg.max_obs_age, shots, helper)


def _obs(args, cfg):
    if args.image:
        return offline_observation(args.image, cfg.runtime / "bin" / "olimar-vision")
    return _dev(cfg, save=True).observe()


def _summary(o) -> dict:
    scr = vision.classify(o)
    d = {"screen": scr, "window": {"id": o.win_id, "points": [o.win_w, o.win_h]}, "ms": {k: round(v, 1) for k, v in o.ms.items()}, "screenshot": [o.shot_w, o.shot_h],
         "shot_path": o.shot_path, "remaining_free": vision.remaining_free(o), "countdown_s": vision.countdown_seconds(o)}
    if scr == "list":
        d["cards"] = [{"title": c.title, "place": c.place, "px": c.line.center} for c in vision.cards(o)]
    if scr == "detail":
        m = vision.mushroom_of(o)
        d.update(size=m.size, kind=m.kind, color=m.color, min_team=m.min_team,
                 ticket_icon=vision.ticket_state(o), players_seen_at_least=vision.players_seen(o))
    if scr == "team":
        d["team_counts"] = vision.team_counts(o)
        d["go"] = vision.go_state(o)
    return d


def cmd_doctor(args, cfg) -> int:
    ok = True

    def chk(name, good, msg=""):
        nonlocal ok
        ok &= bool(good)
        print(f"[{'OK' if good else 'FAIL'}] {name} {msg}")

    chk("Python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    chk("macOS", sys.platform == "darwin")
    chk("swiftc", shutil.which("swiftc"))
    chk("orca CLI", shutil.which("orca"))
    helper = cfg.runtime / "bin" / "olimar-vision"
    try:
        vision.ensure_helper(helper)
        chk("Vision helper 已編譯", helper.exists(), str(helper))
    except vision.VisionError as e:
        chk("Vision helper 編譯", False, str(e))
        return 1
    try:
        dev = OrcaDevice(cfg.bundle, cfg.max_obs_age, cfg.runtime / "shots", helper)  # doctor 的 Orca 診斷永遠用 Orca，避免與下面的原生 daemon 同時存在
        w = dev.window()
        chk("鏡像視窗", True, f"id={w['id']} {w['width']}x{w['height']} pt")
        o = dev.observe()
        chk("截圖長寬比與視窗一致 + 檔案尺寸相符", True, f"{o.shot_w}x{o.shot_h}")
        chk("OCR 有文字", len(o.lines) > 0, f"{len(o.lines)} 行")
        s = vision.classify(o)
        chk("畫面可辨識", s != "unknown", s)
        if s == "ticket_dialog":
            chk("無券視窗", False, "偵測到蘑菇券視窗")
    except Exception as e:
        chk("iPhone 鏡像輸出可觀察", False, f"{type(e).__name__}: {e}")
    try:
        nd = NativeDevice(cfg.bundle, cfg.max_obs_age, None, helper)
        atexit.register(nd.close)
        t = time.perf_counter()
        nd.start()
        chk("原生 daemon（ScreenCaptureKit／Accessibility 權限）", True, f"暖機 {(time.perf_counter() - t) * 1000:.0f}ms")
        o = nd.observe()
        chk("原生截圖+OCR", len(o.lines) > 0, f"capture {o.ms['capture_ms']:.0f}ms ocr {o.ms['ocr_ms']:.0f}ms")
        nd.close()  # 立即結束 daemon，不留背景程序
    except Exception as e:
        chk("原生 adapter", False, f"{type(e).__name__}: {e}（仍可用 --device orca）")
    store = Store(cfg.runtime, cfg.timezone)
    chk("SQLite", True, f"today={store.today()} tz={cfg.timezone} pending={len(store.pending())}")
    return 0 if ok else 1


def cmd_observe(args, cfg) -> int:
    o = _obs(args, cfg)
    print(json.dumps(_summary(o) | ({"lines": [l.__dict__ for l in o.lines]} if args.lines else {}), ensure_ascii=False, indent=1))
    return 0


def cmd_plan(args, cfg) -> int:
    o = _obs(args, cfg)
    r = Controller(None, Store(cfg.runtime, cfg.timezone), cfg).plan(o)
    print(json.dumps(_summary(o), ensure_ascii=False))
    print("\n".join(f"  - {s}" for s in r.steps))
    print(f"=> {r.status}: {r.detail}")
    return 0


def _ctl(cfg) -> Controller:
    return Controller(_dev(cfg, save=False), Store(cfg.runtime, cfg.timezone), cfg)


def cmd_run(args, cfg) -> int:
    if not args.once:
        raise SystemExit("目前只支援 --once")
    r = _ctl(cfg).run_once(args.live)
    print(f"=> {r.status}: {r.detail}" + ("" if args.live else "  （預演，未點擊；加 --live 才會操作遊戲）"))
    if r.timings:
        print("timings(ms):", json.dumps(r.timings, ensure_ascii=False))
    return 0 if r.status in ("dispatched", "planned", "skipped", "no_candidate") else 1


def cmd_smoke(args, cfg) -> int:
    """實機導覽：清單→詳細頁→（僅在無券圖示時）參加→自動→GO 前 guard→取消→回清單。**絕不點 GO**。"""
    r = _ctl(cfg).run_once(True, go=False)
    print(f"=> {r.status}: {r.detail}")
    print("\n".join(f"  - {x}" for x in r.steps))
    print("timings(ms):", json.dumps(r.timings, ensure_ascii=False))
    return 0 if r.status in ("smoke_ok", "no_candidate", "skipped") else 1


def _stats(xs):
    xs = sorted(xs)
    return {"n": len(xs), "min": round(xs[0]), "median": round(statistics.median(xs)), "max": round(xs[-1])}


def cmd_bench(args, cfg) -> int:
    """量測（唯讀＋可逆導覽）：全畫面／ROI 觀察，以及『點卡片→詳細頁就緒→返回→清單就緒』往返（不參加、不 GO）。"""
    from .controller import ROI_DETAIL, ROI_LIST
    ctl = _ctl(cfg)
    dev = ctl.dev
    res: dict = {"device": cfg.device}
    full = []
    for _ in range(args.n):
        t = time.perf_counter()
        dev.observe()
        full.append((time.perf_counter() - t) * 1000)
    res["observe_full_ms"] = _stats(full)
    if cfg.device == "native":
        roi = []
        for _ in range(args.n):
            t = time.perf_counter()
            dev.observe(roi=ROI_LIST)
            roi.append((time.perf_counter() - t) * 1000)
        res["observe_roi_ms"] = _stats(roi)
    if args.nav:
        stack = contextlib.ExitStack()  # 與 live run／watch 互斥：導覽期間不能有另一個程序在操作
        try:
            stack.enter_context(ctl.store.run_lock())
        except LockBusy as e:
            print(f"bench --nav 拒絕執行：{e}", file=sys.stderr)
            return 1
        atexit.register(stack.close)
        ctl.tm, ctl.steps, ctl.t0 = {}, [], time.perf_counter()
        dev.focus()
        c2d, d2l = [], []
        if cfg.device == "orca":
            ctl.cfg.wait_timeout = max(ctl.cfg.wait_timeout, 12)  # Orca 每次點擊約 1.5s、觀察約 0.5s
        try:
            ctl._to_list()
            for _ in range(args.n):
                o = ctl._wait("list", vision.is_list_view, ROI_LIST)
                cs = vision.cards(o)
                if not cs:
                    raise SystemExit("清單上沒有卡片可供 bench 導覽")
                t = time.perf_counter()
                ctl._click_line(o, cs[0].line, "bench 卡片")
                d = ctl._wait("detail", vision.is_detail_view, ROI_DETAIL, stable=True)
                c2d.append((time.perf_counter() - t) * 1000)
                t = time.perf_counter()
                ctl._back(d, vision.is_list_view, ROI_LIST)
                d2l.append((time.perf_counter() - t) * 1000)
        finally:
            dev.restore()
        res["click_card_to_detail_ready_ms"] = _stats(c2d)
        res["click_back_to_list_ready_ms"] = _stats(d2l)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


def cmd_watch(args, cfg) -> int:
    rs = _ctl(cfg).watch(args.live, args.timeout, cfg.runtime / "STOP", args.max_dispatches)
    return 0 if rs and rs[-1].status in ("dispatched", "planned", "skipped", "no_candidate") else 1


def cmd_status(args, cfg) -> int:
    s = Store(cfg.runtime, cfg.timezone)
    print(f"today={s.today()} confirmed_today={s.confirmed_today()} pending={len(s.pending())}")
    end = s.last_expected_end()
    if end:
        print(f"最近一次預計結束 {day_of(end, cfg.timezone)} {time.strftime('%H:%M:%S', time.localtime(end))}（+{cfg.respawn_seconds}s 才可能重生，僅估計）")
    for r in s.recent():
        print(f"#{r['id']} {r['day']} {r['status']:9} {r['mushroom']} 色={r['color']} {r['note'] or ''}")
    return 0


def cmd_reconcile(args, cfg) -> int:
    s = Store(cfg.runtime, cfg.timezone)
    if not s.reconcile(args.id, args.status):
        print(f"#{args.id} 不存在或不是 pending，未更改", file=sys.stderr)
        return 1
    print(f"#{args.id} → {args.status}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="olimar", description=__doc__)
    p.add_argument("--config", help="TOML 設定檔（見 olimar.example.toml）")
    p.add_argument("--device", choices=["orca", "native"], help="覆寫設定檔的 device（native＝快速）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    for name, fn in (("observe", cmd_observe), ("plan", cmd_plan)):
        s = sub.add_parser(name)
        s.add_argument("--image", help="離線分析本地截圖（不碰裝置）")
        if name == "observe":
            s.add_argument("--lines", action="store_true", help="輸出完整 OCR 行")
        s.set_defaults(fn=fn)
    r = sub.add_parser("run")
    r.add_argument("--once", action="store_true")
    r.add_argument("--live", action="store_true", help="明確允許操作遊戲；否則只預演")
    r.set_defaults(fn=cmd_run)
    w = sub.add_parser("watch")
    w.add_argument("--live", action="store_true")
    w.add_argument("--timeout", type=float, default=1800)
    w.add_argument("--max-dispatches", type=int, default=1)
    w.set_defaults(fn=cmd_watch)
    sub.add_parser("smoke", help="實機導覽到 GO 前 guard 後取消（不 GO）").set_defaults(fn=cmd_smoke)
    b = sub.add_parser("bench", help="量測延遲")
    b.add_argument("-n", type=int, default=5)
    b.add_argument("--nav", action="store_true", help="含點擊卡片→詳細頁→返回往返（可逆，不參加）")
    b.set_defaults(fn=cmd_bench)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    rc = sub.add_parser("reconcile", help="人工確認 pending 派遣的實際結果")
    rc.add_argument("id", type=int)
    rc.add_argument("status", choices=["confirmed", "failed"])
    rc.set_defaults(fn=cmd_reconcile)
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    if args.device:
        cfg.device = args.device
    cfg.__post_init__()
    return args.fn(args, cfg)
