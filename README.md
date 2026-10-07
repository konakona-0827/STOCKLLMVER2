# STOCKLLM 單次行情分析

## 多股票掃描（新增）

`python multi_scan.py` 或 `Scan.cmd`：一次登入SKCOM，讀取設定檔的20檔上市股票／ETF盤中零股行情，程式篩選排名後只將Top 10交給OpenAI比較，輸出0～5檔建議並結束。不送單、不模擬成交。
原本 `Start.cmd --symbol 0050` 保留為單股入口。

- 股票池：`config/universe_tw.json`，允許10～50檔，第一版20檔。
- 門檻與分數權重：`config/scan_rules.json`；`CANDIDATE_TOP_N`可設1～10。
- OpenAI比較提示詞：`prompt_selection.md`，未知持倉只允許BUY／WAIT。
- 行情原始資料、全部metrics與分數、Top N排名、OpenAI請求與原始回覆都存於新的`data/analysis/runs/<run_id>/`；`latest_scan.json`指出最新掃描位置。
- 對外單股與多股輸出分開；多股決策是`selection_decision.json`，不覆蓋單股`decision.json`。
- SQLite沿用`market_history.sqlite3`，新增5張scan專用表，保留單股資料表。
- 當日動能=(last-open)/open；區間位置=(last-low)/(high-low)；價差比=(ask-bid)/last。
- 分數=動能＋成交量對數分數＋區間位置分數＋流動性近似對數分數−價差扣分−過期資料扣分。精確公式在`quant_scanner.py`，輸出每個component與規則版本。
- `breakout` component第一版只是區間位置，不是歷史突破。尚無足夠經驗證日K，MA/RSI/ATR等為null，不把盤中快照當日線。
- 盤後LAST_KNOWN可比較但扣分並標記；全UNAVAILABLE輸出NO_VALID_MARKET_DATA，不呼叫OpenAI。

離線測試：`python -m unittest -v test_analysis test_multi_scan`。
2026-10-07實測：一次登入、20/20檔取得、0 unavailable、Top 10送入OpenAI、5檔WAIT、JSON驗證成功，41項測試通過。完整報告位於該次run內的`verification_report.md`。

流程只有：SKCOM → 指定股票market snapshot → OpenAI JSON → 格式驗證 → 顯示建議 → 結束。
不建立Paper Broker，不模擬成交，不更新現金或持倉，不查詢券商庫存，不送單、改單、撤單。

## 使用

在本目錄執行：

```powershell
.\Start.cmd --symbol 0050
```

預設股票為0050。可指定其他上市股票或ETF。
也可以執行 `python main.py --symbol 0050`。程式只分析一次，印出 `REAL ORDER SENT = NO` 後結束，沒有30分鐘排程。

選用：`--held-qty 20` 提供你已知的持倉，`--budget-twd 5000` 提供分析預算。這些只作為本次輸入，不是虛擬帳戶，不會被程式更新。不提供持倉時不產生HOLD／SELL；不提供預算時BUY建議股數為null。
`Stop.cmd` 或Ctrl+C可要求停止，正在執行的外部呼叫需等待返回。

## 行情與建議

使用附帶2.13.59 SKCOM、既有.env。只建立SKCenterLib、SKQuoteLib、SKReplyLib，註冊公告回呼後登入，訂閱市場5的盤中零股。
原始API時間決定行情是否有效；不是用本地取得時間冒充即時行情。

- LIVE：交易時間內且成交時間在120秒內。
- LAST_KNOWN：盤後、較舊行情或明確標示的本地SKCOM快取。畫面與JSON會說明不是即時行情。
- UNAVAILABLE：没有取得或無法驗證行情。強制WAIT，所有價格與股數為null。

只有快照，不宣稱具備新聞、技術指標或歷史趨勢。試撮資訊只會WAIT。
模型confidence為0～1，畫面顯示百分比，並非保證獲利機率。

## 輸出檔案

- `data/analysis/market_snapshot.json`：此次行情。
- `data/analysis/openai_request.json`：實際OpenAI請求內容，含模型、提示詞、完整行情／歷史输入及JSON schema；不含API key。
- `data/analysis/decision.json`：你指定的單一JSON建議格式。
- `data/analysis/openai_response.json`：實際送入資料、原始回覆、API用量或錯誤。
- `data/analysis/connection.json`：去除帳密的COM連線階段／回傳碼。
- `data/analysis/quote_0050.json`：若成功取得SKCOM快照才保存的最後行情。
- `data/analysis/latest_run.json`：最近一次執行ID、各階段時間與該次保存資料夾。
- `data/analysis/runs/<run_id>/`：每次獨立保存的raw_quote、market_snapshot、openai_request、openai_response、decision、connection與run.json，後續執行不覆蓋。
- `data/analysis/market_history.sqlite3`：累積研究資料庫；單股使用 `market_snapshots` / `analyses`，多股使用 `scan_runs` / `multi_market_snapshots` / `quant_metrics` / `candidate_rankings` / `selection_analyses`，GUI 另新增手動配置 `positions` / `position_events`。沒有模擬成交或資金帳本。

時間皆保存含時區的ISO 8601字串（台灣UTC+08:00，日本時間加一小時）：

| 欄位 | 意義 |
|---|---|
| request_started_at | 本次行情請求開始，包含登入與订閱 |
| request_completed_at | 本次行情請求結束 |
| callback_received_at | 收到行情回呼的本地時間；無回呼則null |
| received_at / fetched_at | 讀取券商行情物件的本地時間；快取保留原始取得時間 |
| exchange_time | 券商nTradingDay＋nDealTime提供的成交資料時間，不冒充訂閱或買賣報價更新時間 |
| data_age_seconds | 分析準備時，資料距離券商成交時間的秒數 |
| OpenAI request_started_at / response_received_at | 模型分析請求開始與完成時間，保存於openai_response.json |

原始SDK欄位位於每次的`raw_quote.json/raw_fields`；價格依sDecimal換算，原始整數也保留。
資料庫每次請求各保存一筆，即使重複抓到同一成交時間也不刪除；傳給LLM的`market_history`則按券商時間去重，最多20筆，不把重複快照當成趨勢。只使用該次請求前已取得的歷史，不使用未來資料或快取重複樣本。
`decision.json`保留指定的10個欄位；`openai_response.json`另保存API原始輸出、response object、模型、用量、時間及驗證錯誤。

不讀寫舊的 `data/trading.sqlite3`。舊模擬程式已備份於 `legacy_paper_20261007`，不由目前入口匯入或執行。

2026-10-07已實測成功：SKCOM登入、盤中零股市場訂閱、取得0050最後成交116.2／台灣13:30:00快照、OpenAI回傳LAST_KNOWN及WAIT、印出結果後結束。盤後資料不是即時行情。22項離線測試通過，並核對檔案與資料庫內容一致。

## 測試與依賴

`python -m unittest -v test_analysis` 執行離線驗證，不登入、不呼叫OpenAI、不交易。
`python -m pip install -r requirements.txt` 安裝Python依賴。
SKCOM需完成註冊並有有效登入憑證；OpenAI需要既有.env內的API key。

API錯誤或格式錯誤會顯示WAIT並結束，保留原始回覆供檢查。真實委託與broker cross-check由其他流程處理。

## GUI / 固定自動掃描 / 持倉聯集

雙擊 `Dashboard.cmd`（或 `python dashboard.py`）。原 `Start.cmd` 單股及 `Scan.cmd` 多股入口保持獨立。
GUI 開啟後按「立即重新分析」取得 SKCOM 行情並送到 OpenAI；按 Start Auto Scan 啟用每 1800 秒固定時間格的排程（台北每小時 :00 / :30）。初始自動掃描為關閉。
手動分析有 60 秒冷卻，不影響下一個自動時間。任何分析執行中都不重疊；若自動時間到但仍在分析，跳過該時間格。Stop Auto Scan 只停止後續排程，不中斷本輪保存。關閉視窗會等待當前請求結束並保存。

Position Settings 手動填入 `symbol / user_qty / ai_managed_qty / user_average_cost / ai_average_cost`。成本未知留白；兩種股數均為非負整數。此處是研究配置，不代表券商已確認庫存。空白資料庫不自動建立持倉，測試的 0050 不會寫入正式資料庫。
OpenAI 收到 `Quant Top 10 ∪ ai_managed_qty > 0`，並標記 TOP10 / POSITION / TOP10+POSITION；未入榜或沒有行情的 AI 管理股票也不省略。沒有行情只能 WAIT。
SELL 股數嚴格驗證為 `floor(ai_managed_qty × action_ratio)`，不能加上 user_qty。BUY 未提供資金預算，僅建議方向，股數為 null；HOLD/WAIT 股數為 0。建議不會修改任何配置。
持倉表分開顯示總未實現損益、AI 管理損益與 AI 損益率，成本/價格缺失顯示 `--`；計算依本輪快照，不是持續行情。決策 JSON/理由與各股票行情時間均有獨立分頁。沒有券商成交紀錄時，成交彙總顯示 unavailable。

每輪檔案在 `data/analysis/runs/<run_id>/`：

| 檔案 | 保存內容 |
|---|---|
| run.json | AUTO/MANUAL、started_at、finished_at、行情請求起訖、錯誤、run_id |
| positions_snapshot.json | 本輪讀取的手動持倉配置，不隨後續編輯更動 |
| raw_quotes.json / multi_market_snapshot.json | 原始券商欄位、資料時間、接收時間、請求時間、品質 |
| quant_scan.json / candidate_ranking.json | 分數構成及 Top 10 |
| analysis_set.json | 聯集、來源、量化資訊與持倉/PnL context |
| openai_request.json | 實際提示詞、輸入與 schema，不含金鑰 |
| openai_response.json | 原始 JSON 字串、完整 API 回覆、用量、時間、驗證結果 |
| selection_decision.json | 驗證後的逐檔建議；錯誤時為明確標示的 WAIT fallback |
| dashboard_result.json | 該輪 GUI 使用的完整資料 |
| sqlite_result.json / connection.json | SQLite 核對及 SKCOM 連線事件 |

`data/analysis/latest_dashboard.json` 是最近 GUI 結果索引；歷史 runs 不覆蓋。上述研究資料同時寫入 `market_history.sqlite3`，持倉修改歷程保存在 `position_events`。SQLite 寫入失敗仍盡量保留 run/error JSON。單輪錯誤顯示 ERROR，後續固定自動排程繼續。

提供給下游程式的穩定建議介面為 `data/analysis/latest_advice.json`；每輪同時封存 `runs/<run_id>/advice_interface.json`。內容包含標準化建議、行情品質與年齡秒數、行情／LLM 時間、執行狀態及 `real_order_sent`。欄位說明與 Python 讀取範例見 `ADVICE_INTERFACE.md`。

離線測試：`python -m unittest -q test_analysis test_multi_scan test_dashboard test_advice_interface`。
短週期 GUI：`python dashboard.py --test-mode --interval 120 --data-dir data/gui_test`。
真實 SKCOM/OpenAI 整合驗證：`python verify_dashboard_live.py`，會呼叫 OpenAI API 並開啟 GUI，於獨立 `data/gui_verification/<時間>/` 建立明確標示的 0050/2454 測試配置；完成 MANUAL 和 AUTO 後保存 verification.json 並關閉。不呼叫任何下單 API。
