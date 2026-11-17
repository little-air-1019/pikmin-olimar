import tempfile
import unittest
from pathlib import Path

from .fakes import FakeClock, dialog_screen, detail_screen, list_screen, make, team_screen

CD = 2118  # 戰鬥倒數秒（團隊畫面標題上的『預估 剩下…』）


def game(title="一般 華麗蘑菇", orange=0.0, names=4, remaining=3, go="plain", sel=40, cap=40, dots=None,
         after_go=None, filter_grid=True, joined_to="team", second_dots=None):
    """list → detail → team(0) →(自動)→ team(filled, GO) →(GO)→ after_go。"""
    filled = "filled"
    team_clicks = {"自動": filled, "<BACK>": "detail"}
    s = {
        "list": list_screen(remaining, [(title, "地點", 100)], {title: "detail"}),
        "detail": detail_screen(title, orange, names, joined_to=joined_to),
        "team": team_screen(0, cap, "disabled", team_clicks, dots=dots, grid=0),
        filled: team_screen(sel, cap, go, {"GO": "after_go", "取消": "team", "<BACK>": "detail"}, dots=dots, grid=1),
        "after_go": after_go or list_screen(remaining - 1, [(title, "地點", 100)], {title: "detail"}),
    }
    if dots is not None:  # 有篩選列：點圓點會改變皮克敏格
        for c in dots:
            team_clicks[f"DOT:{c}"] = "team_f"
        s["team_f"] = team_screen(0, cap, "disabled", {"自動": filled}, dots=second_dots or dots, grid=100 if filter_grid else 0)
        if not filter_grid:
            s["team_f"]["grid"] = 0
    return s


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_once(self, screens, live=True, start="list", go=True, **kw):
        ctl, dev, store, clock = make(self.tmp.name, screens, start, **kw)
        r = ctl.run_once(live, go=go)
        return r, dev, store, clock

    # ---- 主流程 ----
    def test_happy_path_confirmed_by_remaining_drop(self):
        r, dev, store, _ = self.run_once(game())
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertEqual(dev.log, ["一般 華麗蘑菇", "參加", "自動", "GO"])
        self.assertEqual(store.confirmed_today(), 1)
        self.assertEqual(dev.events, ["focus", "restore"])
        self.assertIn("discover_to_go_ms", r.timings)
        self.assertGreaterEqual(r.timings["go_clicked"], r.timings["guard_ready"])

    def test_dry_run_never_clicks_or_focuses(self):
        for start in ("list", "detail", "filled"):
            r, dev, *_ = self.run_once(game(), live=False, start=start)
            self.assertEqual((r.status in ("planned", "skipped", "stopped"), dev.log, dev.events), (True, [], []), start)

    def test_poison_selects_white_filter_after_swiping_row(self):
        s = game(title="一般 毒蘑菇", dots={"紅": 500, "黃": 560, "藍": 620})
        # 白色圓點在畫面外：第一次看不到 → 滑動 → 才出現
        s["team"]["clicks"]["<DRAG>"] = "team_scrolled"
        s["team_scrolled"] = team_screen(0, 40, "disabled", {"自動": "filled", "DOT:白": "team_f"}, dots={"紅": 300, "白": 560, "岩": 620})
        s["team_f"] = team_screen(0, 40, "disabled", {"自動": "filled"}, dots={"白": 560}, grid=100)
        r, dev, *_ = self.run_once(s)
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertEqual(dev.log, ["一般 毒蘑菇", "參加", "<DRAG>", "DOT:白", "自動", "GO"])

    def test_color_not_found_after_swipes_stops_before_go(self):
        r, dev, store, _ = self.run_once(game(title="一般 毒蘑菇", dots={"紅": 500, "黃": 560}))
        self.assertEqual(r.status, "stopped")
        self.assertNotIn("GO", dev.log)
        self.assertEqual(store.pending(), [])
        self.assertIn("找不到顏色", r.detail)

    def test_filter_without_visible_effect_is_not_trusted(self):
        r, dev, *_ = self.run_once(game(title="一般 毒蘑菇", dots={"白": 500}, filter_grid=False))
        self.assertEqual(r.status, "stopped")
        self.assertIn("無法驗證篩選", r.detail)
        self.assertNotIn("GO", dev.log)

    def test_color_needs_native_adapter(self):
        r, dev, *_ = self.run_once(game(title="一般 毒蘑菇", dots={"白": 500}), native=False)
        self.assertEqual(r.status, "stopped")
        self.assertIn("native", r.detail)
        self.assertNotIn("GO", dev.log)

    def test_color_mode_auto_skips_filter(self):
        r, dev, *_ = self.run_once(game(title="一般 毒蘑菇"), color_mode="auto")
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertNotIn("<DRAG>", dev.log)

    # ---- 券／競態 ----
    def test_zero_free_stops_without_click(self):
        r, dev, *_ = self.run_once(game(remaining=0))
        self.assertEqual((r.status, dev.log), ("stopped", []))

    def test_ticket_icon_or_five_players_skips(self):
        for kw in (dict(orange=0.08), dict(names=5), dict(orange=0.01)):
            with self.subTest(kw):
                r, dev, *_ = self.run_once(game(**kw))
                self.assertEqual(r.status, "no_candidate")
                self.assertNotIn("參加", dev.log)

    def test_ticket_on_go_after_select_blocks_before_pending_and_go(self):
        """實機競態：加入時免費，選完隊伍 GO 卻出現券圖示。"""
        r, dev, store, _ = self.run_once(game(go="ticket"))
        self.assertEqual(r.status, "stopped")
        self.assertIn("券", r.detail)
        self.assertNotIn("GO", dev.log)
        self.assertEqual(store.recent(), [])  # 連 pending 都沒寫

    def test_plan_reports_stopped_for_non_plain_go(self):
        from olimar.controller import Config, Controller
        ctl, dev, *_ = make(self.tmp.name, game(go="disabled"), start="filled")
        self.assertEqual(ctl.plan(dev.observe()).status, "stopped")
        ctl, dev, *_ = make(self.tmp.name, game(go="plain"), start="filled")
        self.assertEqual(ctl.plan(dev.observe()).status, "planned")

    def test_refused_go_saves_image_for_calibration(self):
        ctl, dev, *_ = make(self.tmp.name, game(go=None))
        saved = []
        dev.save = lambda obs, path: saved.append(str(path))
        r = ctl.run_once(True)
        self.assertEqual(r.status, "stopped")
        self.assertEqual(len(saved), 1)
        self.assertIn("go-refused-unknown", saved[0])

    def test_go_disabled_or_unknown_never_clicks(self):
        for go in ("disabled", None):
            with self.subTest(go):
                r, dev, store, _ = self.run_once(game(go=go))
                self.assertEqual(r.status, "stopped")
                self.assertNotIn("GO", dev.log)
                self.assertEqual(store.recent(), [])

    def test_ticket_race_after_join_clicks_nothing_more(self):
        r, dev, store, _ = self.run_once(game(joined_to="dialog") | {"dialog": dialog_screen()})
        self.assertEqual(r.status, "stopped")
        self.assertEqual(dev.log, ["一般 華麗蘑菇", "參加"])  # 未點確認也未點取消
        self.assertEqual(store.pending(), [])

    def test_ticket_dialog_after_go_never_confirms_and_not_retried(self):
        r, dev, store, _ = self.run_once(game(after_go=dialog_screen()))
        self.assertEqual(r.status, "stopped")
        self.assertEqual(dev.log.count("GO"), 1)
        self.assertNotIn("確認", dev.log)
        self.assertEqual(store.pending(), [])

    def test_small_team_blocks_go(self):
        r, dev, store, _ = self.run_once(game(sel=9))
        self.assertEqual(r.status, "stopped")
        self.assertNotIn("GO", dev.log)
        self.assertEqual(store.recent(), [])

    def test_team_shrinks_between_wait_and_final_guard(self):
        """自動選隊後（ROI 輪詢看到 40/40），GO 前的全畫面重看卻只剩 5 人 → 不 GO。"""
        s = game()
        s["shrunk"] = team_screen(5, 40, "plain", {"GO": "after_go"})
        ctl, dev, store, _ = make(self.tmp.name, s)
        orig = dev.observe

        def observe(roi=None, **k):
            if roi is None and dev.cur == "filled":
                dev.cur = "shrunk"
            return orig(roi=roi, **k)
        dev.observe = observe
        r = ctl.run_once(True)
        self.assertEqual(r.status, "stopped")
        self.assertIn("未達最小", r.detail)
        self.assertNotIn("GO", dev.log)
        self.assertEqual(store.recent(), [])

    # ---- 自我派遣證據 ----
    def test_preexisting_countdown_does_not_prove_dispatch(self):
        """GO 後回到詳細頁，倒數早就在跑（別人先開的），但剩餘次數沒少 → pending。"""
        after = detail_screen("一般 華麗蘑菇", 0.0, 4, cd=CD)
        r, dev, store, _ = self.run_once(game(after_go=after))
        self.assertEqual(r.status, "pending")
        self.assertEqual(len(store.pending()), 1)
        self.assertEqual(dev.log.count("GO"), 1)

    def test_list_count_unchanged_after_go_is_pending_and_restart_blocks(self):
        same = list_screen(3, [("一般 華麗蘑菇", "地點", 100)], {"一般 華麗蘑菇": "detail"})
        r, dev, store, clock = self.run_once(game(after_go=same), confirm_timeout=2)
        self.assertEqual(r.status, "pending")
        ctl2, dev2, *_ = make(self.tmp.name, game(), "list", clock=clock)  # 重啟
        r2 = ctl2.run_once(True)
        self.assertEqual((r2.status, dev2.log), ("stopped", []))
        self.assertIn("pending", r2.detail)

    def test_leave_button_on_detail_confirms(self):
        after = detail_screen("一般 華麗蘑菇", 0.0, 4, cd=CD, leave=True)
        r, dev, store, _ = self.run_once(game(after_go=after))
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertEqual(store.confirmed_today(), 1)

    def test_leave_button_confirms_even_when_detail_labels_are_missed(self):
        from .fakes import L
        after = {"lines": [L("離開", 300, 1000, 80), L("剩下35分14秒", 34, 612, 106, 18)], "clicks": {}}  # classify=unknown
        r, dev, store, _ = self.run_once(game(after_go=after))
        self.assertEqual(r.status, "dispatched", r.detail)

    def test_bare_leave_text_without_battle_context_is_not_evidence(self):
        from .fakes import L
        after = {"lines": [L("確定要離開嗎", 100, 700, 300), L("離開", 300, 1000, 80)], "clicks": {}}
        r, dev, store, _ = self.run_once(game(after_go=after), confirm_timeout=1)
        self.assertEqual(r.status, "pending")

    def test_still_on_team_after_go_is_pending(self):
        s = game()  # GO 後仍在隊伍畫面
        s["filled"]["clicks"]["GO"] = "filled"
        r, dev, store, _ = self.run_once(s, confirm_timeout=1)
        self.assertEqual(r.status, "pending")
        self.assertEqual(dev.log.count("GO"), 1)

    # ---- guard ----
    def test_window_resize_between_observe_and_click_refuses(self):
        ctl, dev, store, _ = make(self.tmp.name, game())
        orig = dev.observe

        def observe_then_resize(*a, **k):
            o = orig(*a, **k)
            dev.win["width"] = 400  # 觀察後視窗被改尺寸
            return o
        dev.observe = observe_then_resize
        r = ctl.run_once(True)
        self.assertEqual(r.status, "stopped")
        self.assertIn("guard", r.detail)
        self.assertEqual(dev.log, [])
        self.assertEqual(dev.events, ["focus", "restore"])  # 失敗也要還原焦點

    def test_start_from_detail_goes_back_first(self):
        r, dev, *_ = self.run_once(game(), start="detail")
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertEqual(dev.log[0], "<BACK>")

    # ---- 掃描 ----
    def test_bounded_horizontal_scan_finds_second_page_card(self):
        a, b = "一般 火蘑菇", "一般 毒蘑菇"
        s = game(title=b)
        s["list"] = list_screen(3, [(a, "甲", 100)], {a: "detail_a", "<SCROLL>": "list2"})
        s["list2"] = list_screen(3, [(a, "甲", 100), (b, "乙", 400)], {a: "detail_a", b: "detail", "<SCROLL>": "list2"})
        s["detail_a"] = detail_screen(a, 0.08)
        s["detail_a"]["clicks"]["<BACK>"] = "list"
        s["detail"]["clicks"]["<BACK>"] = "list2"
        s["after_go"] = list_screen(2, [(b, "乙", 400)], {})
        r, dev, *_ = self.run_once(s, color_mode="auto")
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertEqual(dev.log[:4], [a, "<BACK>", "<SCROLL>", b])

    def test_scan_is_bounded_and_detects_unmoved_carousel(self):
        a = "一般 火蘑菇"
        s = {"list": list_screen(3, [(a, "甲", 100)], {a: "d", "<SCROLL>": "list"}), "d": detail_screen(a, 0.08)}
        s["d"]["clicks"]["<BACK>"] = "list"
        r, dev, *_ = self.run_once(s, max_scrolls=2)
        self.assertEqual(r.status, "no_candidate")
        self.assertLessEqual(dev.scrolls, 2)

    # ---- smoke（導覽到 GO 前 guard，取消，不 GO）----
    def test_smoke_never_clicks_go_and_returns_to_list(self):
        s = game()
        s["list2"] = s["list"]
        r, dev, store, _ = self.run_once(s, go=False)
        self.assertEqual(r.status, "smoke_ok", r.detail)
        self.assertNotIn("GO", dev.log)
        self.assertEqual(store.recent(), [])
        self.assertIn("取消", dev.log)


class Regressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_cancel_only_deselects_then_back_is_used(self):
        """實機：取消只取消選取，之後仍在隊伍頁，必須改用返回鈕，不能無限按取消。"""
        from .fakes import L, H
        s = game()
        s["team"]["lines"].append(L("取消", 300, int(0.9 * H), 80))  # 0/40 時取消仍在（假設它不蓋住返回鈕）
        s["team"]["clicks"]["取消"] = "team"
        ctl, dev, store, _ = make(self.tmp.name, s)
        r = ctl.run_once(True, go=False)
        self.assertEqual(r.status, "smoke_ok", r.detail)
        self.assertEqual(dev.log.count("取消"), 1)
        self.assertEqual(dev.log[-2:], ["<BACK>", "<BACK>"])

    def test_detail_countdown_becomes_expected_end_and_sightings_arm_watch(self):
        title = "一般 華麗蘑菇"
        s = game()
        s["detail"] = detail_screen(title, 0.0, 4, cd=CD)  # 隊伍頁沒有倒數，只有詳細頁有
        ctl, dev, store, clock = make(self.tmp.name, s)
        t0 = clock.now()
        r = ctl.run_once(True)
        self.assertEqual(r.status, "dispatched", r.detail)
        end = store.last_expected_end()
        self.assertAlmostEqual(end, t0 + CD, delta=30)
        self.assertAlmostEqual(r.next_at, end + 300)
        self.assertEqual(len(store.upcoming_ends(0)), 1)  # 看過的倒數也被記錄

    def test_full_mushroom_countdown_is_recorded_for_later_fast_polling(self):
        title = "一般 華麗蘑菇"
        s = game(orange=0.08)
        s["detail"] = detail_screen(title, 0.08, 4, cd=100)
        ctl, dev, store, clock = make(self.tmp.name, s)
        t0 = clock.now()
        r = ctl.run_once(True)
        self.assertEqual(r.status, "no_candidate")
        ends = store.upcoming_ends(t0)
        self.assertEqual(len(ends), 1)
        self.assertAlmostEqual(ends[0], t0 + 100, delta=5)
        # watch：respawn = 結束+300，現在離它很遠 → 慢速；把時間快轉到窗內 → 快速
        from olimar.scheduler import poll_interval
        cfg = ctl.cfg
        self.assertEqual(poll_interval(t0, [ends[0] + 300], cfg), cfg.slow_interval)
        self.assertEqual(poll_interval(t0 + 380, [ends[0] + 300], cfg), cfg.fast_interval)

    def test_back_retry_only_on_timeout_never_after_ticket_warning(self):
        """返回時若出現券警告，必須直接停止，不可因為下一次仍像詳細頁就再點一次返回。"""
        s = game(orange=0.08)
        s["detail"]["clicks"]["<BACK>"] = "warn"
        s["warn"] = dialog_screen()
        ctl, dev, store, _ = make(self.tmp.name, s)
        r = ctl.run_once(True)
        self.assertEqual(r.status, "stopped")
        self.assertIn("券", r.detail)
        self.assertEqual(dev.log.count("<BACK>"), 1)

    def test_back_retry_checks_full_frame_for_ticket_text_outside_roi(self):
        """返回逾時、且券警告只出現在全畫面（ROI 看不到），重試前必須全畫面確認，不可再點。"""
        s = game(orange=0.08)
        s["detail"]["clicks"]["<BACK>"] = "detail"  # 返回被吃掉
        s["detail"]["lines"] = [l for l in s["detail"]["lines"]]
        ctl, dev, store, _ = make(self.tmp.name, s)
        from .fakes import L
        orig = dev.observe

        def observe(roi=None, **k):
            o = orig(roi=roi, **k)
            if roi is None and dev.log.count("<BACK>") >= 1:  # 全畫面才看得到警告
                o.lines.append(L("需要蘑菇券", 100, 700, 300))
            return o
        dev.observe = observe
        r = ctl.run_once(True)
        self.assertEqual(r.status, "stopped")
        self.assertEqual(dev.log.count("<BACK>"), 1)  # 沒有重試點擊

    def test_ticket_modal_outside_roi_stops_before_any_mutating_click(self):
        """券警告在 ROI 之外、底下隊伍標題仍可見：ROI 輪詢會通過，但全畫面複查必須擋下，不能點篩選／自動。"""
        from .fakes import L
        s = game(title="一般 毒蘑菇", dots={"白": 500})
        ctl, dev, store, _ = make(self.tmp.name, s)
        orig = dev.observe

        def observe(roi=None, **k):
            o = orig(roi=roi, **k)
            if dev.cur in ("team", "team_f", "filled") and roi is None:  # 只有全畫面才看得到 modal 文字
                o.lines.append(L("需要蘑菇券", 100, 300, 300))
            return o
        dev.observe = observe
        r = ctl.run_once(True)
        self.assertEqual(r.status, "stopped")
        self.assertIn("券", r.detail)
        self.assertEqual(dev.log, ["一般 毒蘑菇", "參加"])  # 參加之後沒有任何點擊（沒有 DOT／自動／GO）
        self.assertEqual(store.recent(), [])

    def test_ticket_modal_outside_roi_after_filter_blocks_auto(self):
        from .fakes import L
        s = game(title="一般 毒蘑菇", dots={"白": 500})
        ctl, dev, store, _ = make(self.tmp.name, s)
        orig = dev.observe

        def observe(roi=None, **k):
            o = orig(roi=roi, **k)
            if dev.cur == "team_f" and roi is None:
                o.lines.append(L("需要蘑菇券", 100, 300, 300))
            return o
        dev.observe = observe
        r = ctl.run_once(True)
        self.assertEqual(r.status, "stopped")
        self.assertEqual(dev.log, ["一般 毒蘑菇", "參加", "DOT:白"])  # 篩選後遇到 modal：不點自動

    def test_ticket_appears_on_detail_between_roi_and_join_click(self):
        s = game()
        ctl, dev, store, _ = make(self.tmp.name, s)
        orig = dev.observe

        def observe(roi=None, **k):
            o = orig(roi=roi, **k)
            if roi is None and dev.cur == "detail":  # 全畫面複查時參加鈕已出現券圖示
                o.colors = lambda r: {"orange": 0.08, "red": 0, "green": 0, "total": 1}
            return o
        dev.observe = observe
        r = ctl.run_once(True)
        self.assertEqual(r.status, "stopped")
        self.assertNotIn("參加", dev.log)

    def test_delayed_carousel_update_finds_new_free_candidate(self):
        a, b = "一般 火蘑菇", "一般 毒蘑菇"
        s = game(title=b)
        s["list"] = list_screen(3, [(a, "甲", 100)], {a: "detail_a", "<SCROLL>": "list_mid"})
        s["list_mid"] = list_screen(3, [(a, "甲", 100)], {a: "detail_a"})  # 拖曳後輪播還沒動
        s["list2"] = list_screen(3, [(a, "甲", 100), (b, "乙", 400)], {a: "detail_a", b: "detail"})
        s["detail_a"] = detail_screen(a, 0.08)
        s["detail_a"]["clicks"]["<BACK>"] = "list"
        s["detail"]["clicks"]["<BACK>"] = "list2"
        s["after_go"] = list_screen(2, [(b, "乙", 400)], {})
        ctl, dev, store, _ = make(self.tmp.name, s, color_mode="auto")
        orig, n = dev.observe, [0]

        def observe(roi=None, **k):
            if dev.cur == "list_mid":
                n[0] += 1
                if n[0] > 3:  # 舊畫面被看到 3 次之後才更新
                    dev.cur = "list2"
            return orig(roi=roi, **k)
        dev.observe = observe
        r = ctl.run_once(True)
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertGreaterEqual(n[0], 3)

    def test_partial_swipe_same_keys_moved_counts_as_progress_then_new_card(self):
        """半張卡寬的拖曳：同一張卡（key 相同）只是左移，不能當成盡頭；再拖一次才出現新卡。"""
        a, b = "一般 火蘑菇", "一般 毒蘑菇"
        s = game(title=b)
        s["list"] = list_screen(3, [(a, "甲", 300)], {a: "detail_a", "<SCROLL>": "list_p"})
        s["list_p"] = list_screen(3, [(a, "甲", 120)], {a: "detail_a", "<SCROLL>": "list2"})  # 同 key，x 左移 180px
        s["list2"] = list_screen(3, [(a, "甲", 10), (b, "乙", 400)], {a: "detail_a", b: "detail"})
        s["detail_a"] = detail_screen(a, 0.08)
        s["detail_a"]["clicks"]["<BACK>"] = "list"
        s["detail"]["clicks"]["<BACK>"] = "list2"
        s["after_go"] = list_screen(2, [(b, "乙", 400)], {})
        ctl, dev, *_ = make(self.tmp.name, s, color_mode="auto")
        r = ctl.run_once(True)
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertEqual(dev.scrolls, 2)

    def _inertia_game(self, positions):
        a, b = "一般 火蘑菇", "一般 毒蘑菇"
        s = game(title=b)
        s["list"] = list_screen(3, [(a, "甲", 300)], {a: "detail_a", "<SCROLL>": "anim"})
        s["anim"] = list_screen(3, [(a, "甲", 300)], {a: "detail_a"})
        s["detail_a"] = detail_screen(a, 0.08)
        s["detail_a"]["clicks"]["<BACK>"] = "list"
        s["detail"]["clicks"]["<BACK>"] = "list2"
        s["list2"] = list_screen(3, [(b, "乙", 150)], {b: "detail"})
        s["after_go"] = list_screen(2, [(b, "乙", 150)], {})
        ctl, dev, store, clock = make(self.tmp.name, s, color_mode="auto")
        orig, i = dev.observe, [0]

        def observe(roi=None, **k):
            if dev.cur == "anim":  # 慣性動畫：每次輪詢卡片位置都不同，key 也先變
                x = positions(i[0]); i[0] += 1
                title = "一般 毒蘑菇"
                dev.screens["anim"] = list_screen(3, [(title, "乙", x)], {title: "detail"})
            return orig(roi=roi, **k)
        dev.observe = observe
        return ctl, dev

    def test_inertia_animation_waits_until_settled_then_uses_final_position(self):
        """實機：翻頁後約 2 秒才停穩，中途位置持續在變；必須等停穩才點擊，不能把移動中當盡頭或直接點。"""
        ctl, dev = self._inertia_game(lambda n: max(150, 450 - 40 * n))
        r = ctl.run_once(True)
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertEqual(dev.log[:4], ["一般 火蘑菇", "<BACK>", "<SCROLL>", "一般 毒蘑菇"])

    def test_carousel_that_never_settles_stops_instead_of_clicking_or_ending(self):
        ctl, dev = self._inertia_game(lambda n: 100 + 30 * (n % 7))
        r = ctl.run_once(True)
        self.assertEqual(r.status, "stopped")
        self.assertIn("沒有停穩", r.detail)
        self.assertEqual(dev.log, ["一般 火蘑菇", "<BACK>", "<SCROLL>"])
        self.assertNotIn("一般 毒蘑菇", dev.log)

    def test_boundary_jitter_is_not_progress(self):
        a = "一般 火蘑菇"
        s = {"list": list_screen(3, [(a, "甲", 300)], {a: "d", "<SCROLL>": "list_j"}),
             "list_j": list_screen(3, [(a, "甲", 303)], {a: "d", "<SCROLL>": "list"}), "d": detail_screen(a, 0.08)}
        s["d"]["clicks"]["<BACK>"] = "list"
        ctl, dev, *_ = make(self.tmp.name, s, max_scrolls=3)
        r = ctl.run_once(True)
        self.assertEqual(r.status, "no_candidate")
        self.assertEqual(dev.scrolls, 1)  # 抖動不算進展，第一次拖曳後就判定盡頭

    def test_carousel_that_never_moves_ends_within_bound(self):
        a = "一般 火蘑菇"
        s = {"list": list_screen(3, [(a, "甲", 100)], {a: "d", "<SCROLL>": "list"}), "d": detail_screen(a, 0.08)}
        s["d"]["clicks"]["<BACK>"] = "list"
        ctl, dev, *_ = make(self.tmp.name, s, max_scrolls=2, scroll_settle=1.0)
        r = ctl.run_once(True)
        self.assertEqual(r.status, "no_candidate")
        self.assertLessEqual(dev.observations, 150)  # 2 次捲動 × (1s/0.01s 輪詢) 之內，不會無限等

    def test_expected_end_uses_detail_capture_time_not_selection_time(self):
        s = game()
        s["detail"] = detail_screen("一般 華麗蘑菇", 0.0, 4, cd=CD)  # 隊伍頁沒有倒數
        ctl, dev, store, clock = make(self.tmp.name, s)
        orig, seen = dev.observe, {}

        def observe(roi=None, **k):
            if dev.cur == "detail" and "t" not in seen:
                seen["t"] = clock.now()  # 詳細頁被觀察的時間
            return orig(roi=roi, **k)
        dev.observe = observe
        for _ in range(20):  # 選隊期間流逝了時間（模擬點擊／動畫）
            pass
        orig_click = dev.click

        def slow_click(*a, **k):
            clock.sleep(2.0)
            return orig_click(*a, **k)
        dev.click = slow_click
        r = ctl.run_once(True)
        self.assertEqual(r.status, "dispatched", r.detail)
        self.assertAlmostEqual(store.last_expected_end(), seen["t"] + CD, delta=0.5)  # 不含後來流逝的秒數

    def test_invalid_config_is_rejected(self):
        from olimar.controller import Config
        with self.assertRaises(ValueError):
            Config(color_mode="verfiy")
        with self.assertRaises(ValueError):
            Config(device="fast")


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_watch_times_out_and_polls_slowly_when_no_estimate(self):
        a = "一般 火蘑菇"
        s = {"list": list_screen(3, [(a, "甲", 100)], {a: "d", "<SCROLL>": "list"}), "d": detail_screen(a, 0.08)}
        s["d"]["clicks"]["<BACK>"] = "list"
        ctl, dev, store, clock = make(self.tmp.name, s)
        rs = ctl.watch(True, timeout=100, stop_file=Path(self.tmp.name) / "STOP")
        self.assertGreaterEqual(len(rs), 2)
        self.assertLessEqual(len(rs), 5)  # slow_interval=30s
        self.assertTrue(all(r.status == "no_candidate" for r in rs))

    def test_watch_polls_fast_near_estimated_respawn(self):
        a = "一般 火蘑菇"
        s = {"list": list_screen(3, [(a, "甲", 100)], {a: "d", "<SCROLL>": "list"}), "d": detail_screen(a, 0.08)}
        s["d"]["clicks"]["<BACK>"] = "list"
        ctl, dev, store, clock = make(self.tmp.name, s)
        did = store.begin_dispatch("x", None)
        store.finish(did, "confirmed", "", clock.now() - 300 + 5)  # next_at = now+5：正處於快速輪詢窗
        rs = ctl.watch(True, timeout=60, stop_file=Path(self.tmp.name) / "STOP")
        self.assertGreater(len(rs), 10)  # fast_interval=0.5s（每輪還含捲動等待）→ 60s 內多次

    def test_watch_rejects_nonpositive_dispatch_limit_before_any_click(self):
        ctl, dev, store, clock = make(self.tmp.name, game())
        for n in (0, -1):
            with self.assertRaises(ValueError):
                ctl.watch(True, 100, Path(self.tmp.name) / "STOP", max_dispatches=n)
        self.assertEqual(dev.log, [])

    def test_watch_stop_file(self):
        ctl, dev, store, clock = make(self.tmp.name, game(orange=0.08))
        stop = Path(self.tmp.name) / "STOP"
        stop.write_text("")
        self.assertEqual(ctl.watch(True, 300, stop), [])

    def test_watch_stops_after_max_dispatches(self):
        ctl, dev, store, clock = make(self.tmp.name, game(remaining=5))
        rs = ctl.watch(True, timeout=3000, stop_file=Path(self.tmp.name) / "STOP", max_dispatches=1)
        self.assertEqual([r.status for r in rs], ["dispatched"])
        self.assertEqual(dev.log.count("GO"), 1)


if __name__ == "__main__":
    unittest.main()
