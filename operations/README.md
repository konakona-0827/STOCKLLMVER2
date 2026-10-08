# 交易執行與健康檢查

Dashboard 開啟時立即與券商對帳；有未結委託時每 1 分鐘再查，其他時候每 5 分鐘再查，包括盤外與週末。按「重新整理並與券商對帳」、每輪分析開始前與送單後也會立即對帳。健康檢查、AUTO/MANUAL 行情掃描與真實委託共用同一條 COM 工作執行緒及 Capital session；工作會在佇列等待，不會平行重複登入或平行送單。

經獨立程序對照，完整 Order/Reply 登入流程若在登入後才建立 `SKQuoteLib`，即使只訂閱 1 檔也回 3030；先建立 `SKQuoteLib` 再執行 Center 登入，則同一完整流程可取得行情。正式程式現在於登入前建立並保留 Quote 物件。原本 21 檔單次訂閱也回 3030，因此清單依序切成每批最多 20 檔；每批間確認 `LeaveMonitor()=0`，再讓同一 Quote 物件重入。手動持股仍納入行情快照供持倉市值顯示，但不因此加入 AI 管理持倉。獨立程序已以正式類別連續完成兩次 20＋1 檔掃描：共四次訂閱及四次離開都回 0，每次取得 21 檔且只登入一次。

SDK 文件指出 `SKQuoteLib_LeaveMonitor` 也會中斷 Reply 連線。多批行情掃描結束後，broker runtime 會在同一 COM 執行緒確認並恢復 Reply 連線；恢復失敗就視為掃描錯誤。訂閱前以唯讀 `SKQuoteLib_GetQuoteStatus` 記錄連線數與超限旗標；訂閱回傳 3030 時會再查一次。`data/health/capital_com_events.csv` 記錄 PID、Capital session、COM object ID、建立呼叫點、Login API/旗標/回傳碼，以及 Quote monitor、訂閱和離開呼叫。程式碼沒有 `SKCenterLib_LoginSetQuote` 呼叫點。Dashboard 關閉時會在 broker COM 執行緒離開 Quote monitor。若首次 Center 登入已嘗試但後續初始化失敗，本次 Dashboard 執行期間不再重試登入。

## 紀錄位置

- `operations/issue_log.csv`：每項程式問題的發現時間、完成時間、修改與驗證。開始修改前新增一列，完成後補齊。
- `data/health/health_checks.csv`：每次健康檢查一列，可用 Excel 等試算表開啟。
- `data/health/inventory_reconciliation.csv`：每次各股票的正式 AI 記帳股數、手動持股設定與券商現股庫存。對帳以「正式 AI 持股 + 已設定手動持股」比較券商總庫存；尚未歸屬的多出股數標記 `BROKER_EXCESS_UNATTRIBUTED`，不自動寫入 `ai_positions`。手動持股數低於設定值時標記 `CONFIGURED_EXCEEDS_BROKER`。
- `data/health/runtime.log`：每次檢查一行的引擎紀錄。
- `data/health/latest_health.json`：最新完整檢查結果，包括券商查詢失敗原因與未解決委託。

每次對帳使用共用 Capital session 重新查詢可買金額與現股庫存，核對正式 `ai_positions`、人工設定持股及正式委託紀錄，並把時間、查詢狀態、差異和正式委託狀態寫入 `live_execution.sqlite3` 的 `broker_refresh_snapshots`。App 的「券商持倉對帳」與「正式委託與成交紀錄」分頁顯示同一份快照；券商查詢不完整時標記未驗證。對已有券商序號的委託，會對照 Reply/OrderReport 可取得的成交證據；若完整券商回放能以精確序號核對 BUY 或 SELL 成交價量，且券商現股庫存足以涵蓋更新後 AI 與人工持股，會在同一 SQLite 交易中補記確認成交的股數、委託狀態與稽核事件。券商以精確序號確認取消時也會記錄已取消餘量並結束未結狀態。重複對帳僅補記新增事件。證據不足時不推定成交，也不根據券商總庫存猜測 AI 股數。

券商 Reply 的成交事件若能以委託序號精確核對，健康快照另記成交股數、價格與未含手續費的成交價金；App 主畫面與正式委託分頁分別顯示合計與逐筆明細。`daily_buy_reserved_twd` 是當日 BUY 風控額度占用，包含已成交與未結委託的委託價金及費用／緩衝額，並非券商或銀行的實際交割款。`latest_health.json` 在 OneDrive 目錄遇到暫時性的 Windows 存取鎖時會使用獨立暫存檔重試；若仍無法寫入，券商結果會保留於正式資料庫的對帳快照並在 App 標記檔案寫入警告。

App 的「持倉概況」只列券商庫存對帳資料，不顯示舊的模擬持倉或模擬成交分頁。重新整理時另外查詢持股行情；只有對帳已驗證且每檔持股的行情在 120 秒內仍為 `LIVE`，才顯示合計持倉參考市值。行情缺漏或過期時，介面提示待更新行情並保留對帳時間；介面每 30 秒重算行情時效。這個參考市值不會改寫券商餘額或正式持股股數。

每輪 AI BUY 預算取三者最低值：(1) 券商新鮮當日可買額扣除最低保留額與未結 BUY 的額外安全保留；(2) AI 資金總上限扣除已持倉投入估算與未結 BUY 安全保留；(3) 當日 BUY 上限扣除正式資料庫內已占用的額度。AI 資金總上限是累計限制，不會每輪重置。已持倉投入以券商成交價與費用緩衝保守估算；缺少成交價時使用 BUY 委託限價，無法重建時停止新 BUY。此金額與各項來源會傳入 AI 請求，並顯示於分析結果。分析驗證以委託 ask 價和費用預留核對全部 BUY；超過預算則該輪結果標為錯誤、回傳全 WAIT，不呼叫實盤執行器。送單前重新對帳並縮小預算，執行器內再查券商、帳本與總上限；單筆送單前仍查一次券商可買額。

歷史分析失敗或有未結委託會使整體健康狀態顯示 `WARN`；只要本次券商餘額、庫存、精確序號事件、未結 BUY 保留與 AI 累計投入都可核對，仍可依扣除保留後的正數預算繼續新 BUY。任何目前的券商查詢錯誤、帳本差異或預算來源缺漏仍會阻擋 BUY；不可僅憑舊的 `WARN` 狀態把有效餘額歸零。

健康快照的 `buy_block_reasons` 說明新買單暫停原因；`current_buy_budget_twd=0` 表示本程式暫停新 BUY，不代表券商可買額或銀行現金為零。券商事件推定狀態區分已受理但未見成交、部分成交且餘量未確認、全部成交、取消及本次無事件；`ALERT` 用於券商確認成交多於正式帳本等實際差異，單純有可核對的未結委託時顯示 `WARN` 並另外保留未成交額度。

資金畫面直接列出 `GetBalance` 的一戶通餘額、可動用／可出金金額、當日可買進金額，以及 `GetT3DueAmt` 的台幣 T/T-1/T-2 交割日應收付淨額。交割應付款是券商預計值，不代表銀行已扣款，也不從券商「當日可買進金額」重複扣減。未結買單保留使用未成交股數乘委託限價再加費用緩衝，屬額外保守風控，券商本身可能已保留部分金額；畫面會標明此差異。

檢查使用 `GetBalance`、`GetRealBalanceReport` 和正式 SQLite 帳本；只有精確序號核實的券商成交或取消才會修改正式帳本。正式交易紀錄位於 `data/execution/live_execution.sqlite3`；合成 LLM 測試使用 `synthetic_execution.sqlite3`。舊的 `execution.sqlite3` 原樣保留，作為歷史資料，不再供正式交易讀取。若券商庫存小於 `ai_positions`，標記 `AI_EXCEEDS_BROKER`。`ACKNOWLEDGED`、`PARTIALLY_FILLED`、`UNCONFIRMED` 仍列為待追蹤；健康檢查不會根據券商總庫存自動猜測 AI 持倉。委託回報不等於成交。

## 預算保護

在 `config/execution_live.json` 修改：

- `min_available_to_buy_twd`：預設 `10000`。每筆 BUY 預留後，券商可買金額至少要保留此數；定期對帳確認低於此數時停止 AUTO，餘額回升後需手動重新按「開始自動掃描」。
- `max_order_twd`：預設 `10000`，單筆委託價金上限。
- `max_daily_buy_twd`：預設 `20000`，台北時間同一天所有已進入送單階段 BUY 的預留金額上限；送單前以 SQLite 交易鎖定並再次驗算。
- `buy_cash_buffer_rate`：預設 `0.01`。
- `min_buy_fee_reserve_twd`：預設 `20`。每筆費用預留取價金的 1% 與 20 元較高者；券商實際最低手續費仍應按帳戶合約確認。

每筆 BUY 使用限價乘股數計算價金，先用一批開始時的券商可買金額，再於每筆送單前重新呼叫 `GetBalance`。兩者及本批已預留金額一起限制可用額。若資料不足、格式不可信、當日額度不足或會觸及最低餘額，該筆 BUY 不送出。

現有資料庫的副本可由 `python -m operations.backup_current_state` 建立，放在 `data/backups/`。程式不會因為找不到 `live_execution.sqlite3` 就默默建立一個新的正式交易庫；應先確認原因或從備份還原。備份不包含帳密環境檔。
