# 新 Skill 正式執行器整合驗證

2026-11-25。新分支 `codex/production-trading-skill` 從使用中的最新基底 `1f3a56265a3440f7aad5ff00b7d67020772058ce` 建立，保留原全市場篩選、資金／資料／經濟風控、執行器、券商會話、成交回寫、持倉及退出、帳戶鎖與恢復機制。這次建立可由使用者啟動的 Live 入口，沒有啟動新分支 Live 或送出真實委託。

## 實際通過的驗證

| 類別 | 證據 |
|---|---|
| 完整離線回歸 | 959 tests，OK；`full_regression.txt` |
| 最後介面更新 | 38 tests，OK；`ui_tests.txt` |
| 新 Skill／執行器／逾時專項 | 11 tests，OK；涵蓋研究與反證實際 prompt、pretrade／exit provider、舊批准隔離、SDK 委託欄位、部分成交 2 股、防重送及零買額度下退出 |
| Skill 格式及編譯 | quick_validate 與 compileall 通過 |
| 真實 Native CLI | `gpt-6-sol`，2 個凍結歷史 packet × Baseline／Skill × 研究／反證，共 8 次，全部取得合法回覆 |
| 真實 pretrade 傳輸 | 同模型、同 Skill、原 CodexClient，空交易契約 1 次成功；不是買進批准 |
| 真實券商唯讀對帳 | 11/25 17:29 台灣時間，庫存核實通過，錯誤 0，預算 READY；未送單 |
| 全市場資料準備 | 1,990 檔上市櫃普通股，已更新至 11/24；11/25 待原啟動更新流程取得 |

原執行器整合使用模擬券商驗證，確認 `SendStockOddLotOrder` 的股票、買賣別、盤中零股、股數與價格正確；僅確認的部分成交 2 股進入隔離 AI 帳本，再執行相同 decision 不產生第二次送單。賣出鏈也通過原介面／退出測試。這是工程證據，沒有冒充真實券商受理或成交。

## 決策及盈利證據的界線

兩個 Native packet 依「固定檔案排序的第一個資料案例」及「存在原合格數值路徑的第一個案例」選取，沒有使用後續損益選樣；全部呼叫保留。兩個版本收到相同 packet、資金／價格／成本／schema／模型，Skill 僅增加決策引導。第一個案例 Baseline 為 WATCH／WATCH、Skill 為 REJECT／WATCH；第二個兩組均 WATCH／WATCH。它們的即時資料限制與研究理由保留在原本機紀錄。

8 次研究呼叫已回報 input 128,408 tokens、output 6,755；另一次空交易 pretrade 契約 input 13,179、output 42。共 **9 次，input 141,587、output 6,797**，缺用量 0。使用既有 ChatGPT 方案，額外 API 帳單 US$0，方案 credits 經濟成本未知。兩組研究平均延遲約 Baseline 19.543 秒、Skill 20.348 秒；只有兩個資料案例，不能當成穩定效能結論。

這些是開發期凍結資料的模型及契約驗證，不是新分支首日前向報酬。新分支尚無實盤成交，因此 Net Return、Win Rate、Expectancy、Profit Factor、Maximum Drawdown、Sharpe、指數相對報酬及機率校準都沒有可據以認定優勢的新實盤樣本。LLM confidence 不是勝率。

前一場 85 截點／`gpt-5.6-luna` 的正式 MD 策略重播、以及持續待命的 Paper 配對試驗，保持原版本及帳戶。不能將其績效當作本次 FLOW／`gpt-6-sol` 新實盤候選的績效，也不能把不同架構直接混算為同條件 A/B。

## 修改與已修正問題

- `skills/tw-production-decision/SKILL.md`：台股多階段決策適配，沿用 host schema、可觀測證據、原數值路徑與權限。以本次對兩個 MIT 專案及既有系統的研究為方法來源：[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)、[trader-skill](https://github.com/Jakeschincariol/trader-skill)。原 Time Machine／市場適配研究仍在既有研究實驗，沒有把模擬頻率直接當實盤勝率。
- `skill_profile.py`、`config/trading_skill.json`：固定 repository Skill 與 LF 正規化 SHA；未知設定、版本或內容不符停止；載入不授予券商權限。
- `review.py`、`pipeline.py`、`state.py`：研究與反證載入同一 Skill，保存快照及呼叫識別，cache 加入 Skill 身分。關閉 Skill 的 Baseline 控制維持舊 cache key。
- `legacy_runtime.py`、`execution_bridge.py`：下單前及持倉 provider 載入 Skill；交付原執行器前核對 Skill 身分，不能混用舊批准。原 `execution/` 下單實作未改。
- `bounded_cli_capture.py`、`codex_cli.py`：檔案取代 stdout pipe，避免後代程序占住 pipe；逾時有界且只清理本次模型程序。pretrade／exit 鎖定目前 FLOW 模型，避免 CODEX_MODEL 環境值使比較或審查換模型。回報缺 token 保持未知。
- 實測發現 Windows 測試還原空 Git 設定環境值後，子程序可能缺值；Git 測試與歷史 baseline 讀取改以完整環境字典傳入，保留原設定，未移除認證或來源檢查。補上舊手工研究 fixture 的新 Skill 識別，未放寬正式 gate。
- `RunSkillTrading.ps1`、兩個 cmd、`skill_readiness.py`、介面標題：提供 Check／只讀／Live 入口與清楚狀態，沿用原恢復流程。CI 增加 Windows 離線 gate；本次未宣稱遠端 CI 已執行或已 push。

## 實際資金與帳務

既有帳戶上限 NT$10,000、最低保留 NT$5,000、日額 NT$30,000；買單預留 max（1%，NT$20）與最低佣金 NT$1 分開。11/25 唯讀對帳時狀態 PENDING_ORDER、庫存已核實、錯誤 0。實際可新買額度以交易電腦重新對帳結果為準，不能把既有投入清零後再撥 NT$10,000。

對帳經原流程追加券商快照並刷新 latest-health 指標；未改實盤啟用設定、未清除歷史決策／成交／未結單、未送單。`execution_live.json` SHA 與本輪開始時取得的資金規則 receipt 一致。新分支 Live 仍須在啟動後取得當前券商、報價、完整日線及原批准。

## 明天仍要觀察

11/26 是尚未發生的首日前向日。必須確認 11/25 資料更新、非試撮零股行情、兩方意見、原數值及事件檢查、pretrade、委託受理／未成交／部分成交、核實回寫、跨日可賣與退出。沒有訊號時保留原因，不為製造交易降低門檻。

新買零股不適用當沖，首日主要是運行、決策與未實現損益觀察；完整進出場須跨交易日。[證交所說明](https://academy.twse.com.tw/page/library/trade/5.html)。正式新版本尚未實盤受理／成交驗收，不能宣稱盈利能力已證明。

使用方式見根目錄 `TRADING_SKILL.md`。當前隔離 Paper 的版本／PID、首日報告位置、來源與測試更新另記於既有 `STOCKLLM/reports/paper_first_day_20261125/`。
