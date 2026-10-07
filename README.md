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
- `data/analysis/market_history.sqlite3`：累積研究資料庫，只有`market_snapshots`與`analyses`兩張資料表，沒有訂單／資金／持倉表。

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
