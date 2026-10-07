# STOCKLLM 模擬版

群益 SKCOM 盤中零股行情 → 真實 OpenAI API → BUY / SELL / HOLD → 本地模擬委託與成交。
使用提供的 SKCOM 登入與唯讀行情／庫存查詢；沒有真實下單程式或切換實盤的參數。

## 啟動與停止

在本目錄執行 `Start.cmd`，停止時執行 `Stop.cmd`。停止訊號會在當前 HTTP 請求結束後處理，寫入中的 SQLite 交易會完成。
需要 Python 3.10 以上。需先註冊 x64 SKCOM，並具備有效憑證與 API 權限；其他環境先執行 `python -m pip install -r requirements.txt`。

```powershell
cd C:\Users\ho.chouchuan\Documents\HO\project\personalmono\STOCKLLM
.\Start.cmd
# 另一個視窗停止：
.\Stop.cmd
```

`python main.py --once`：透過 SKCOM 抓行情；取得券商行情後呼叫一次真實 LLM；不模擬成交，不占用正常時段的 cycle。盤後可使用，但模型會看到 STALE。
`python main.py --status`：不連網，查看最近一次 cycle 與模擬庫存。
`python -m unittest -v test_paper`：離線測試，合成行情與測試成交僅寫入暫存資料庫。

## 看到的結果

- `llm_wants_order`：LLM 是否要求 BUY / SELL。
- `simulated_order_sent`：本地檢查是否接受並記錄模擬委託。
- `outcome`：`PAPER_FILLED`、`NO_ORDER`、資金／庫存拒絕或行情不足等原因。
- `real_order_sent`：永遠是 `false`。
- `source`：`LLM`、既有提示詞的停損停利規則 `PROMPT_RISK_RULE`，或錯誤退回 `FALLBACK`。

最近輸出在 `data/latest_decision.json`，狀態在 `data/health.json`。
完整輸入、原始模型輸出、模型名稱、token 用量、決策、委託、模擬成交、錯誤都在唯一的 `data/trading.sqlite3`。
價格欄位以元表示；`estimated_value`、`*_cents` 以分表示。

## 保留的策略與明確限制

`prompt.md` 作為實際 system instructions；依使用者要求改為零股股數決策，其餘觀察與風控原則保留。沒有找到可驗證的舊 STOCKLLM 台股模擬交易程式。
相鄰 `stockprototype/stockprototype/llm.py` 提供 Responses API 呼叫模式參考，沿用該專案的預設模型 `gpt-5.6-luna`。
相鄰 `stockprototype/stockprototype/simulation.py` 與 2026-09-10 紀錄是歷史方向預測，並非這份台股策略的成功交易驗證，未冒稱重用其交易邏輯。

提示詞與 JSON schema 使用 `qty` 表示實際股數，每筆 BUY/SELL 限 1～999 股；HOLD 為 0 股。1 代表 1 股，不再乘以 1,000。
固定模擬本金 10,000 元，預設觀察 `2330,2317,2882` 是可修改的範例名單，並非找回的舊選股結果。
每檔傳給 LLM 的 `sizing` 含模擬買賣估價、含手續費的可買股數上限、可賣股數、庫存成本與買入預算。LLM 根據行情、費用、現金及持倉提出適合的股數，並解釋買入總支出或賣出淨收入與剩餘部位；上限不代表應買滿。多筆委託仍逐筆重算本地資金限制。賣出可以部分減碼，停損停利也以實際股數處理。
可在啟動前設定 `PAPER_SYMBOLS`（逗號分隔的上市普通股四位代碼）與 `OPENAI_MODEL`，不需要改動密鑰。

保留提示詞的 10 筆／300 秒觀察、單檔 60%、總曝險 90%、停損 3%、停利 6%、回撤 5% 暫停新買條件。
回撤限制只拒絕該輪 BUY，之後仍照常呼叫 LLM；停損停利每個策略輪檢查，並非即時保證。
模擬成本：買價賣一加一檔，賣價買一減一檔，手續費 0.1425% 最低 20 元，賣出稅 0.3%。
接受後以該估價立即完成理想化模擬成交，不代表真實撮合、排隊、部分成交或流動性結果。
只有本資料庫的模擬 BUY 成交增加可賣持倉。

## 行情、排程與錯誤

行情只來自 `capital.py` 的群益 SKCOM：登入 → 註冊公告／帳號／行情回呼 → EnterMonitorLONG → 等待3003商品就緒 → RequestStocksWithMarketNo(1, 5, symbols) → LONG回呼 → GetStockByIndexLONG。
每30秒保存快照，空閒時持續PumpEvents；不使用公開網站備援。`nTradingDay`／`nDealTime`形成行情時間，試撮資料不成交。來源錯誤或舊 TWSE 快照不送進 LLM。
每60秒嘗試更新唯讀券商庫存，帳號與登入ID會排除。LLM可看庫存狀態／時間／股票股數，但不會將其加入模擬可賣持倉。
成交價缺失時保持空值；價差與觀察報酬使用買賣報價，明確標示為盤中快照，沒有虛構日線／新聞／財報。
同一股票同一 exchange timestamp 不重複計為新觀察。
使用 **Asia/Taipei (UTC+8)** 的 09:00、09:30 … 13:00 時段；日本時間晚一小時。
盤中啟動會處理當前半小時 slot，不補跑過去 slot；SQLite 保證每個 slot 最多一次，包括啟動後崩潰或 API 失敗。
周末不建立一般 cycle；休市日／暫停交易依真實報價時間判斷，沒有新鮮行情則記錄 SKIPPED，不呼叫付費 LLM。
超過 120 秒或未來時間的行情標示 STALE，不模擬成交。行情失敗僅可使用仍在120秒內的 SKCOM 快照；沒有 SKCOM 資料就 SKIP，不會向 LLM 傳假行情。COM／登入失敗每60秒重試，不停止常駐主循環。

LLM timeout／JSON 錯誤記錄 HOLD，下一 slot 繼續。API SDK 不自動重試，避免同輪額外付費呼叫。
進程鎖僅防同時啟動兩份程序，崩潰後 OS 自動释放；不是 persistent error block。
`.env` 原檔不改，支援目前 API key 換行格式；券商帳密只用於 SKCOM 登入，不傳給 LLM、不寫入本程式資料庫；SDK 自行產生的原生日誌由券商元件管理。

API 格式依據：[OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。
行情介面依據：提供的 `策略王COM元件使用說明_V2.13.59.docx`，以及 `掛單測試/capital_oddlot_probe_simple_fixed.py` 的登入／回呼模式。

## SKCOM 註冊與唯讀診斷

`python capital_check.py` 檢查本機 DLL 與 COM 物件；不登入。
`python capital_check.py --connect` 只做一次登入、憑證讀取、帳號查詢、即時庫存查詢及行情訂閱；不送單、改單或刪單。
診斷結果在 `data/capital_check.json`。獨立診斷不呼叫 LLM；正式模擬主流程則會提供去除帳號的庫存觀察資訊，不匯入模擬帳本。
庫存查詢必須收到 `OnRealBalanceReport` 的 `##` 結尾才標示完成，查詢失敗不解讀為零庫存。
行情必須有訂閱回呼與成功快照；收到快照也不表示一定是當下交易，需看其中交易日期／時間。

2026-10-07 本機實測：Python x64、已安裝 comtypes 1.4.17，SDK x64 型別庫可載入；
四個 COM 類別均回傳 `0x80040154`（未註冊），因此登入、券商行情和券商庫存尚未能驗證。
`.env` 既有 DLL 路徑不存在，診斷工具會尋找本目錄已解壓的 2.13.59 對應位元 DLL，不修改 `.env`。

啟動新版主流程前，先對 **SetupCapital.cmd 按右鍵→以系統管理員身分執行**。
此檔僅以 SDK 原本的 regsvr32 方法註冊 x64 SKCOM.dll，不登入、不下單。
完成後執行 `python capital_check.py --connect`。目前執行環境沒有管理員權限，所以尚未執行系統註冊。
憑證必須安裝於實際登入的 Windows 使用者環境；本次尚未能確認其有效性，註冊完成不等於登入一定成功。
依賴安裝：`python -m pip install -r requirements-capital.txt`。
