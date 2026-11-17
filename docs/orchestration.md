# Agent 協作紀錄

使用 Orca 公開 CLI 的 `orchestration` 管理實作任務與後續 review/fix。首輪 Run：`run_2ccf25d44053`；實作 Task：`task_e780f8622f4d`，Dispatch：`ctx_4d784669f1d2`。

第一輪：30 項測試通過、實機 doctor/observe/plan 成功，但實際選隊樣本暴露辨識缺口，不能執行 GO。Codex plugin 真實 review 找到 GO 券圖示未檢查、既有倒數被誤判派遣成功、reconcile 未限制 pending，以及未匿名測試資料。原始完整報告：`runtime/codex-review.txt`。修正與速度優化 Task：`task_819d6bbe4d88`，沿用同一 Claude session，不另起重複 editor。

第二輪加入常駐 Swift daemon、ScreenCaptureKit 截圖、局部 Vision OCR、原生 Quartz 點擊，以及 icon-only 顏色與 GO 狀態判斷。實機可滑動清單並跳過滿員候選，尚未按 GO。Codex 第二次 review 報告 `runtime/codex-review-2.txt` 找到兩項 P2：Orca 截圖時間戳記必須在處理前記錄，以及非法 `color_mode` 必須拒絕。協調者另找到倒數誤從沒有倒數的選隊頁讀取，導致重生快速輪詢無法啟動。

Claude 在同一實作任務內完成多輪修正，累計六次真實 Codex plugin review，原始報告依序為 `runtime/codex-review.txt` 到 `runtime/codex-review-6.txt`。第二輪任務交付時有 60 項測試通過，仍留下 ROI 外券彈窗及捲動過早停止的問題；因此尚不可當作已通過 live 驗收。最後集中修正 Task：`task_33cf29fd8ef9`，Dispatch：`ctx_a7158aa77bb2`，沿用原 Sonnet session。最後的小範圍修正由協調者獨立檢查及驗證；不把先前 review 報告當作最終版本無缺陷的證明。

原預備 Task `task_dcbeb9b1d14b` 未派遣，因修正已併入前一輪而由最後任務取代。Orca 沒有取消狀態，紀錄以 `failed` 與 `supersededBy` 明確標示取代關係；不是實作或測試失敗。

實機收尾另有兩個集中修正：`task_056967141dad` 修正同一批卡片位移卻被判成到盡頭；`task_a1f65416b090` 實測捲動慣性需 0.99–2.4 秒，將停穩預算改為 4 秒並在未停穩時停止。水平滾輪的 14 種實機嘗試沒有可靠效果，已移除實驗程式，保留經實測的原生拖曳。最後版本由協調者獨立執行 **70 項測試全部通過**；原生 doctor 通過。原生全畫面觀察中位數 251 ms、局部觀察 170 ms（各 3 次暖機樣本，`runtime/bench-root.json`）；單次點擊 55–65 ms、開卡到詳細頁約 1.9 秒屬先前實機量測。沒有把元件速度當成發現到 GO 的完整速度。

協調者 60 秒 live 監控檢查 3 張候選，均因券圖示跳過，沒有 GO、confirmed 或 pending。紀錄為 `runtime/live-validation.log`。最後一次重測因鏡像顯示「iPhone 使用中／請鎖定 iPhone 來連接」而以 unknown 停止，未點擊；`runtime/live-validation-3.log`。**免費 GO、選色、GO 後確認的完整實機流程仍未驗證**，plain GO 判斷與部分顏色仍需要首次有效樣本校準。

所有已派遣任務均已收尾並提出 worker-release；原 Claude terminal 為外部既有資源，Orca 保留供使用者使用，沒有未決的自建 reclaimable terminal。

Claude 啟動命令：

```sh
claude --model claude-sonnet-5-5 --advisor claude-opus-5-5 --permission-mode acceptEdits
```

專案設定 `.claude/settings.json` 固定主模型與 advisor，不修改全域模型預設。2026-09-29 實際 TUI 顯示 `Sonnet 5.5 · Claude Team`，並在實作前顯示 `Advising using Opus 5.5`。模型名稱由實際 session 驗證；不是只靠傳入參數推定。

Advisor 設定方法及相容性：[Claude Code 官方 advisor 文件](https://code.claude.com/docs/en/advisor)。

本機已安裝啟用 `codex@openai-codex` 1.0.5，使用 plugin 的原生 working-tree review。review 是獨立只讀階段；需要修正時另派 Claude fix 任務，測試後再 review。

```sh
node /Users/twinb00550336/.claude/plugins/cache/openai-codex/codex/1.0.5/scripts/codex-companion.mjs review '--wait --scope working-tree'
```

實機畫面與原始 review 存在 gitignored `runtime/`。可重用的測試資料請裁切／去除私人名稱或使用 OCR 記錄，不應把完整地點、玩家名稱與手機狀態截圖提交到 repo。
