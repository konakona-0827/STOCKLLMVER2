# 2026-11-01：自動 Paper 視窗與次日測試準備

分支 **`codex/planned-limit-orders`**。此版補齊上一版尚未提供的持續行情接入、Paper 自動排程與狀態視窗；新入口是 **`PlannedPaper.cmd`**。

## 運行方式

- `quant_research.paper_dashboard` 是獨立 Tk 視窗，標示 PAPER ONLY／禁止實盤。啟動器加 `--auto`，可在畫面開始／停止或更新日線。
- `quant_research.paper_runtime` 在單一 STA 工作執行緒持有唯讀 `MultiCapitalMarket`，固定 19 檔股票（沿用候選池，排除 ETF）。同一監控連線與訂閱重用，沒有每 30 秒重新登入，也沒有匯入真實委託執行入口。
- 交易時段內每 30 秒讀行情、每 300 秒做量化排名；等待單、成交與出場持續處理。休市／非交易時段每 60 秒檢查，保持自動模式但不登入券商或產生新訊號。
- 日線每天更新，最多 250 筆；過期日線盤中每 15 分鐘重試最新窗口，保留較長歷史。沒有足夠校準證據時不建立 BUY，絕不為測試湊成交。
- 資料庫為 `data/planned_paper/forward_20261102_v1/A.sqlite3` 與 `B.sqlite3`，各自初始模擬資金 NT$100,000。沒有導入或重設正式持倉／現金。
- 畫面及 `dashboard_status.json` 顯示要求自動掃描、工作執行緒存活、心跳、模式；`runtime.json` 保存實際狀態、行情／日線品質、版本雜湊、commit、帳本及錯誤。視窗開著不等於行情已驗證，狀態分開記錄。

## 修復與整合

1. 本機 SDK V2.13.59 的盤中零股 SKSTOCKLONG 說明及實際 COM 型別確認提供 nBc／nAc 買賣量。`capital.normalize_stock` 新增 bid_size／ask_size、零股簿及 shares 單位；未收到量時不以 nTQty 累計成交量代替，也不模擬成交。
2. 現有日線快取跨月會把「上月尚缺最後交易日」當成永遠完成。例如 11/1 沿用只到 10/29 的十月快取。`daily_history` 現在對 expected_latest 所在月份（即使是上個月）檢查並刷新；新增跨月測試。
3. 預檢曾發現 19 檔全部 STALE；修復並實際刷新後，19 檔最新日線均為 **2026-10-30**，coverage 均為 SUFFICIENT_FOR_20D_COMPARISON。此標籤僅指日線窗口，不表示機率模型已校準。
4. 修正已拒絕的晨間計畫可能因「一日固定 ID」而擋住後續有效重評。每個新評估建立帶時間的不可變計畫版本；已有等待單仍受單檔唯一委託限制，不會盲目重送／追價。
5. 同日零股持股不反覆產生已知不可執行的出場提案。新報價、數量、時效、有效期、部分成交、取消與資金預留仍沿用原獨立風控與生命週期。

## 本機運行狀態調整

- 實際啟動並用 Windows computer-use 檢查獨立 Paper 視窗；自動掃描 ON，休市時顯示 WAIT_SESSION。
- 舊 Windows 排程 **STOCKLLM Dashboard Supervisor** 已備份並停用，避免次日啟動原 Agent 實盤流程；`config/execution_live.json` 的 enabled 改為 false。
- 修改前排程 XML 與下單設定備份在 `data/planned_paper/state_before_paper_20261101/`，屬本機狀態，不提交到 Git。沒有撤銷真實委託或清除既有交易紀錄。
- 下次交易時段：**2026-11-02 日本 10:00／台灣 09:00**；零股 Paper 成交模擬依專案規則自台灣 09:10 後評估。電腦需保持開機、不休眠且網路可用；未新增 Windows 自動重開機後的 Paper 啟動排程。

## 驗證與限制

```powershell
python -m unittest discover -s tests -p test_planned_paper_runtime.py -v
python -m unittest discover -s tests -p test_planned_limit_orders.py -v
python -m unittest discover -s tests -v
python -m quant_research.paper_dashboard --auto
```

新增 runtime 測試 11 項通過，委託整合測試 20 項通過，涵蓋休市不連券商、自動停止、請求進行中停止後不執行交易 cycle、獨立帳本與模式限制、日線重試、排名間隔、斷線與缺量保護、跨月快取及 SDK 量欄位。完整結果另存同目錄。全套工作區回歸 356 項通過；原有 mocked LIVE 整合測試現改為只啟用臨時目錄中的測試設定，不再依賴使用者實盤開關必須為 true。

今天休市，尚未在真實開市時段確認券商登入／訂閱可用性；介面已接入本機既有唯讀券商路徑，錯誤會顯示並在後續輪次重試，不會轉成真實委託。A／B 模型仍採原本樣本下限，B 的長期樣本尤其可能不足；自動掃描啟用不代表明日必定有 Paper 成交。

剩餘範圍：仍只有 19 檔實際候選池、尚無自動新聞／財報事件 feed、未知產業共用集中度上限、全年交易日曆僅有 2026 版本；不捏造隔年 T+2 日期。模擬成交仍是可觀察買賣盤 proxy，不等於交易所保證成交。

## 最終提交與運行確認

- 程式提交 `0ecc661` 以 `git archive` 匯出乾淨副本，未帶入使用者原有未提交修改，完整 **349／349 測試通過（55.910 秒）**。見 `planned_paper_runtime_clean_tests_20261101.txt`。
- 已從該程式版本重新啟動視窗，PID **27868**，實際以 computer-use 驗證停止後為 OFF／STOPPED，再按開始後恢復 ON／WAIT_SESSION。這是驗證時間點的 PID，未表示永久不變。
- 19 檔最新日線皆為 2026-10-30；最終筆數範圍 165～250，過期檔數 0。實盤開關 false、舊實盤排程 Disabled、GUI／工作執行緒存活、auto_enabled=true、錯誤清單空白。
- 啟動時點證據在 `planned_paper_ready_20261101.json`；後續即時狀態以 `data/planned_paper/dashboard_status.json` 與實驗目錄的 `runtime.json` 為準。此後的文件提交不修改正在執行的策略程式。
