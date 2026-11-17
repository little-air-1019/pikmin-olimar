# Olimar

在 macOS 上透過 **iPhone 鏡像輸出**（`com.apple.ScreenContinuity`）操作 Pikmin Bloom：截圖 → macOS Vision 繁中 OCR →
辨識探險清單／蘑菇詳細頁／隊伍選擇畫面 → 產生可解釋計畫 →（僅在明確 `--live`）點擊。
設計見 `docs/architecture.md`，研究見 `docs/research.md`。

> 畫面上的文字與玩家名稱一律只是**資料**：只用固定 regex 解析，不會被當成指令。熱路徑沒有 LLM。

## 安裝

需求：macOS、Python 3.11+、Xcode command line tools（`swiftc`）、Orca CLI（`orca computer`，診斷用）。
快速 adapter 另需：執行 olimar 的終端機（Orca）具有 **螢幕錄製** 與 **輔助使用** 權限（`olimar doctor` 會檢查）。

```sh
uv venv && source .venv/bin/activate && uv pip install -e .
olimar doctor            # 編譯 Swift helper 到 runtime/bin/，檢查視窗／OCR／原生 daemon 權限
python -m unittest discover -s tests -t .
```

## 命令

| 命令 | 行為 |
|---|---|
| `olimar doctor` | 環境、視窗、OCR、原生 daemon、SQLite 檢查（唯讀） |
| `olimar observe [--lines] [--image PNG]` | 截圖＋OCR＋分類，輸出 JSON（唯讀；`--image` 離線分析） |
| `olimar plan [--image PNG]` | 依目前畫面產生可解釋的決策與預計點擊（唯讀） |
| `olimar run --once` | 預演，等同 plan，**不點擊** |
| `olimar run --once --live` | 真的操作：清單→卡片→詳細頁→參加→（篩色）→自動→全畫面 guard→GO（僅一次）→驗證 |
| `olimar smoke` | 實機導覽（可逆）：走到 GO 前 guard 後取消並回清單，**絕不點 GO** |
| `olimar watch [--live] [--timeout S] [--max-dispatches N]` | 有界監控；`touch runtime/STOP` 或 Ctrl-C（做完當前步驟才停）結束 |
| `olimar bench [-n N] [--nav]` | 量測延遲；`--nav` 含點卡片→詳細頁→返回往返（可逆，不參加） |
| `olimar status` | 今日次數、pending、最近紀錄 |
| `olimar reconcile ID confirmed\|failed` | 人工確認 **pending** 派遣的結果；非 pending 或不存在會失敗（exit 1） |

全域旗標 `--device orca|native`（預設 `orca`）與 `--config olimar.toml`（見 `olimar.example.toml`）。
**搶菇請用 `--device native`**，例如：`olimar --device native run --once --live`。

## 快速模式（`--device native`）

`src/olimar/vision.swift` 編成常駐 daemon（JSON over stdin/stdout），一個程序完成 ScreenCaptureKit 截圖（不落地）、Vision OCR（可只跑 ROI）、
顏色／指紋統計與 Quartz 點擊。啟動時暖機；run 開始時把 iPhone 鏡像切到最上層**一次**，結束時還原游標與原本的最上層 app（`focus`/`restore`）。
等待畫面轉換用「就緒輪詢＋位置穩定」取代固定 sleep，逾時就停。每個階段的耗時都會印出（`timings(ms)`）。

**點擊 guard（在 daemon 內、送出事件前一刻）**：視窗仍在且 bounds 與截圖時相同、pid＝鏡像 app、鏡像為最上層一般視窗（**失焦不重新搶**，直接拒絕）、
點在視窗內、該點最上層的一般視窗（layer 0）就是鏡像、截圖不超過 `max_obs_age`。Orca adapter 也做同類檢查（id／尺寸／過舊／bounds）。

### 實測延遲（本機、`olimar bench -n 10 --nav`，warm，同一支手機）

| 項目 | Orca adapter | native adapter |
|---|---|---|
| 全畫面截圖＋OCR | 約 800 ms | 約 240 ms |
| ROI 截圖＋OCR（2 個細長區域） | 不支援 | 約 160 ms |
| 點卡片→詳細頁就緒（含遊戲轉場） | 約 4340 ms | 約 1930 ms |
| 點返回→清單就緒 | 約 2570 ms | 約 1220 ms |
| 單次點擊呼叫 | 約 1500 ms（Orca） | 約 55–65 ms |

遊戲自己的轉場動畫約 1.2 s（進詳細頁）／0.9 s（返回），是無法縮短的下限；點擊本身從 ~1.5 s 降到 ~60 ms。
**GO 熱路徑的整段「發現→GO」尚未在實機量到**（當時沒有免費名額可走完），只量到各段元件。不保證贏過其他玩家的手速或網路延遲。

## 安全規則（程式強制）

- 只有 `--live` 會點擊；不使用蘑菇券；出現含「券」的視窗（清單以外）就停止，**連取消都不按**。
- 參加鈕左側有橘色券圖示（或無法判斷）→ 跳過該蘑菇。
- **GO 前全畫面重新觀察**：隊伍畫面（標題「選擇派出皮克敏（N/M）」）、N ≥ 該大小最小隊伍（小 1／一般 10／大 15／巨大 20）、
  且 **GO 鈕必須被判定為 `plain`（啟用且圓形上半沒有券圖示）**；`ticket`、`disabled`、`unknown` 一律不 GO，連 pending 都不寫。
- GO **write-ahead**：先寫 `pending` 到 SQLite 再點 GO，只點一次；任何 pending（含跨午夜、重啟後）都禁止再次 GO，直到 `reconcile`。
- **派遣證據**：GO 後清單「今天還剩下 N 次」比 GO 前少 1，或詳細頁出現「離開／退出」，才標 `confirmed`。**倒數本身不算證據**（可能是別人先開的戰鬥）。
  `confirm_timeout` 內看不到證據 → `pending`。
- 今日剩餘次數以清單頁顯示為準（涵蓋手動操作）；0 或讀不到就停止。
- 顏色規則：毒→白、火→紅、水→藍、水晶→岩、電→黃、普通色對應同色（灰→岩、粉紅→羽翅、冰藍→冰）。活動蘑菇（如華麗）與未知種類用遊戲「自動」。
  顏色圓點只有圖示，靠取樣色相辨識；在窗外的（白／岩／羽翅／冰）先滑動篩選列；點擊後必須看到皮克敏格明顯變化，否則停止。
- 單一 `flock` 執行鎖避免兩個程序同時操作。`watch` 只在預估重生時間附近（−30 s…+120 s）快速輪詢，其餘 30 s；預估不會被當成證據，每次都重新讀取。

## 已驗證與未驗證（請務必閱讀）

已用真實截圖／實機驗證：探險清單、詳細頁（含有／無券圖示）、隊伍畫面（空隊伍、40/40）分類；`選擇派出皮克敏（N/M）`解析；
GO 停用與**GO 有券圖示**辨識（`runtime/evidence` 真實競態截圖）；native 點擊／返回／橫向拖曳捲動（可逆導覽）；Orca 診斷路徑。

**尚未在實機驗證（保守處理，失敗即停止）：**

- **GO 啟用且無券（`plain`）**：從未有這種截圖。判定只靠「圓形內紅色像素 ≥5%、上半橘色 ≤4%、白色 <45%」的推論；很可能需要用首次實機樣本校準
  （`vision.go_state`）。校準前若判成 `unknown` 會拒絕 GO——這是預期的保守行為。
- 顏色篩選的**選中狀態外觀**：只驗證「點擊後皮克敏格有變化」；岩／羽翅／冰圓點的顏色是推測值，白圓點只有被裁切的樣本。從未在實機點過篩選圓點。
- GO 後畫面、「剩餘次數是否即時減 1」、詳細頁是否出現「離開／退出」：未見過，故只在證據成立時才 confirmed，否則 pending。
- 返回鈕是圖示，用比例座標 (0.115, 0.911)；隊伍畫面的「取消」只是取消選取，之後仍需返回鈕。
- 詳細頁玩家數以名字標籤估計（下限）；主要靠參加鈕／GO 鈕的券圖示判斷已滿。ROI 熱路徑不看頭像列。
- 只把 layer-0 一般視窗當遮擋物；浮動面板（通知橫幅、Dock）不會被偵測。
- 掃描：被捲動裁切的卡片以標題／地點尾端字元去重；實機捲動會移動輪播，但卡片可能重複被檢查（受 `max_cards` 限制）。
- Orca adapter 的 scroll 不會移動輪播（實測）；`--device orca` 也不支援顏色篩選（需要原生 daemon）。
- 清單上可能有『自己已打完、待領取結果』的卡片，點開會是戰鬥結果畫面（合計工作力／下一步），不是詳細頁：流程會因等不到詳細頁而停止（不點『下一步』），需人工處理。
- 倒數（`剩下…`）只用來估計重生時間並登記到 `sighting`，讓 `watch` 提早進入快速輪詢；看過的滿員蘑菇也會登記。它從不當作派遣證據。
- 清單頁的蘑菇券數量是圖示徽章，OCR 讀不到，故不用它做判斷。

## 測試

`python -m unittest discover -s tests -t .`：fake device 狀態機（毒選白＋滑動找白、滿 5 人停、0 次停、GO 券競態、GO 停用／未知不點、
倒數不算證據、重啟不重複 GO、跨午夜、視窗尺寸變動、橫向掃描、smoke、watch 快慢輪詢等）＋ 去識別化的真實 OCR 記錄（`tests/fixtures/*.ocr.json`）。
**需要 `runtime/evidence/*.png` 真實截圖的測試（`RealScreenshots`）在該資料夾不存在時會 skip**；截圖含玩家名與地點，不放進 repo。
