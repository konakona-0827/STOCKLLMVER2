# 2026-11-02：承接原正式帳戶的計畫限價委託

分支 `codex/planned-limit-orders`。使用者已明確選擇「是，授權真實資金限價委託」，並要求不使用獨立帳本。此版回到原 Dashboard 與 BrokerRuntime，不再使用 Paper 視窗作為本次測試入口。

## 承接範圍

- 原 `data/execution/live_execution.sqlite3`：AI 管理持股、正式委託、逐筆核對與成交歷史。
- 原 `data/analysis/market_history.sqlite3`：人工持股歸屬、策略資金總上限、掃描與決策歷史。
- 原 `config/execution_live.json`：最低餘額、每日上限、單筆限制、手續費預留與行情時效。只增加可明確關閉的 planned_limits_enabled；不提高既有額度或重設資金。
- 原選定策略與 Agent 模式繼續使用，只新增進場定價語義；不把 A／B 比較帳本或預設研究資金混進正式帳戶。
- 停止先前獨立 Paper 視窗；其既有研究紀錄保留，沒有刪除或轉成正式成交。尚未提交的帳戶橋接方案已撤回。

## 改修

1. 正式候選在啟用設定時標記 PLANNED_LIMIT，Prompt 說明 suggested_price 為願意買進的委託上限，不要求現價已經到達。
2. 淨報酬與風險預算改以指定買價評估，仍保留雙邊費用、稅、買賣價差與原本報酬風險門檻；不再用 ask 覆蓋指定買價。
3. Advice Interface 保存 entry_plan：指定買價、目標、失效價、建立時間、當日 13:30 台灣時間到期、版本與理由；Broker 送單前核對此契約、時段、價格及失效條件。
4. 新模式僅支援 BUY 的計畫定價；SELL 保持既有可賣量、持股歸屬與退出檢查，不因這次修改繞過人工持股保護。
5. 同標的已有 STARTED／SEND_PENDING／SUBMITTED／ACKNOWLEDGED／PARTIALLY_FILLED／UNCONFIRMED 時，不接受另一筆 BUY 或 SELL，先完成對帳。
6. 仍使用原 CapitalOddLotExecutor、ExecutionStore 與既有券商回報：先持久化預留與 SEND_PENDING、接受後記錄委託，僅依精確序號的確認成交更新持倉；部分成交與未知回報不盲目重送。
7. entry_plan 與 pricing_policy 同時存入送單前、接受回報與最後委託紀錄，即使程序中斷也不丟失原計畫。
8. 正式 Dashboard 標示「正式帳戶・計畫限價自動交易」，原持倉／預算／交易紀錄畫面不另造替代資料。`FormalPlanned.cmd`／`RunFormalPlanned.ps1` 為明確的原帳戶入口。

## 驗證

```powershell
python -m unittest discover -s tests -p test_formal_planned_entry.py -v
python -m unittest discover -s tests -v
```

新增 5 項測試通過，包括成本按指定價格計算、時段／到期／改價／失效拒絕、股票與 ETF tick、設定預設關閉，以及原正式帳本的端到端測試：現價 100、限價 98.5，實際傳給 mocked 券商的仍為 98.5；接受後持倉維持 0，核對精確委託序號的 5 股成交後才增加為 5。原既有持倉保持原數量；同 decision_id 及不同 run_id 的重複委託均不得重送。所有測試使用臨時 DB 與 mocked 下單 API，不使用真實資金測試單。

完整工作區回歸 362 項通過。另修復舊 Paper 測試時鐘與當日真實日期耦合的問題，避免跨到測試日期時把模擬上午報價與真實凌晨時間混用。

股票／ETF 價格單位核對[證交所交易制度](https://www.twse.com.tw/zh/products/system/trading.html)及 [ETF 規則](https://wwwc.twse.com.tw/zh/products/securities/etf/overview/rules.html)。

## 執行界線

- 本次正式限價為當日有效 ROD，不追價、不建立實驗帳本、不強制為了測試成交。正式自動掃描沿用原 30 分鐘排程及 5 分鐘相位（一般首輪台灣 09:05／日本 10:05），未變更為 Paper 的 30 秒掃描。
- 入場前會拒絕已跌破失效價或逾期的計畫。**已受理等待中的原生限價單不是條件單**；不宣稱內含自動停損或新增盤中自動撤單。盤中撤單必須經券商接受與原精確序號對帳確認後才釋放未成交預留。ROD 剩餘量依交易日失效與既有對帳處理。
- 不會因新定價模式放寬淨收益、報酬風險比、每筆模型損失、最低保留額或成本檢查。資金小、費用高或資料不足時仍可能沒有委託。
- Repository 預設 enabled=false／planned_limits_enabled=false；本機才依明確授權啟用。正式帳戶備份、授權與持倉／額度核對證據保存在忽略的 data 目錄，不將個人財務資料推送至 Repository。

## 最終驗證與啟動

- 程式提交 `0f23538` 的乾淨 Git 匯出副本完整測試 **354／354 通過（31.257 秒）**；工作區測試為 362／362。輸出見 `formal_planned_clean_tests_20261102.txt`。
- 正式資料庫已用 SQLite backup 備份，本機持股列、資金上限列與原風控值經前後比對不變。只啟用本機 enabled／planned_limits_enabled 兩項授權開關；沒有建立新帳戶或發送湊數測試單。
- 已關閉獨立 Paper 視窗，恢復原 STOCKLLM Dashboard Supervisor 排程並啟動正式 Dashboard。畫面確認自動掃描啟用，2026-11-02 台灣 09:05／日本 10:05 進行第一個原排程時段。
- 啟動後 2026-11-02 台灣 08:22 的正式券商核對為 OK、inventory_checked=true、errors=[]。目前畫面使用原正式資金、持倉及歷史，而不是研究預設值。
- 本機詳細證據在 `data/backups/formal_live_activation_20261102/`，包括啟用前後配置／持倉核對與使用者授權；這些個人財務資料未加入 Git。此後的文件提交不修改已啟動的策略程式。
