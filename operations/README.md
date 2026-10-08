# 交易執行與健康檢查

Dashboard 在台北時間 08:00–14:00 啟動或運行時做健康檢查，包含 08:00 與 14:00，每個整點一次；盤外、週末會等待下一個交易日 08:00。健康檢查、AUTO/MANUAL 行情掃描與真實委託共用同一條 COM 工作執行緒及 Capital session；工作會在佇列等待，不會平行重複登入或平行送單。行情掃描先檢查既有登入與回報通道，沿用 Center/Reply，只建立當次 Quote 監聽。若首次 Center 登入已嘗試但後續初始化失敗，本次 Dashboard 執行期間不再盲目重試登入；錯誤寫入掃描與健康檢查紀錄，需確認券商連線後重啟 Dashboard。

## 紀錄位置

- `operations/issue_log.csv`：每項程式問題的發現時間、完成時間、修改與驗證。開始修改前新增一列，完成後補齊。
- `data/health/health_checks.csv`：每次健康檢查一列，可用 Excel 等試算表開啟。
- `data/health/inventory_reconciliation.csv`：每次各股票的正式 AI 記帳股數、手動持股設定與券商現股庫存。對帳以「正式 AI 持股 + 已設定手動持股」比較券商總庫存；尚未歸屬的多出股數標記 `BROKER_EXCESS_UNATTRIBUTED`，不自動寫入 `ai_positions`。手動持股數低於設定值時標記 `CONFIGURED_EXCEEDS_BROKER`。
- `data/health/runtime.log`：每次檢查一行的引擎紀錄。
- `data/health/latest_health.json`：最新完整檢查結果，包括券商查詢失敗原因與未解決委託。

檢查使用 `GetBalance`、`GetRealBalanceReport` 和唯讀 SQLite 查詢。正式交易紀錄位於 `data/execution/live_execution.sqlite3`；合成 LLM 測試使用 `synthetic_execution.sqlite3`。舊的 `execution.sqlite3` 原樣保留，作為歷史資料，不再供正式交易讀取。使用者已確認新正式庫開始時沒有既有 AI 持股。若券商庫存小於 `ai_positions`，標記 `AI_EXCEEDS_BROKER`。`ACKNOWLEDGED`、`PARTIALLY_FILLED`、`UNCONFIRMED` 仍列為待追蹤；健康檢查不會根據券商總庫存自動改寫 AI 持倉。委託回報不等於成交。

## 預算保護

在 `config/execution_live.json` 修改：

- `min_available_to_buy_twd`：預設 `10000`。每筆 BUY 預留後，券商可買金額至少要保留此數；整點健康檢查確認低於此數時停止 AUTO，餘額回升後需手動重新按「開始自動掃描」。
- `max_order_twd`：預設 `10000`，單筆委託價金上限。
- `max_daily_buy_twd`：預設 `20000`，台北時間同一天所有已進入送單階段 BUY 的預留金額上限；送單前以 SQLite 交易鎖定並再次驗算。
- `buy_cash_buffer_rate`：預設 `0.01`。
- `min_buy_fee_reserve_twd`：預設 `20`。每筆費用預留取價金的 1% 與 20 元較高者；券商實際最低手續費仍應按帳戶合約確認。

每筆 BUY 使用限價乘股數計算價金，先用一批開始時的券商可買金額，再於每筆送單前重新呼叫 `GetBalance`。兩者及本批已預留金額一起限制可用額。若資料不足、格式不可信、當日額度不足或會觸及最低餘額，該筆 BUY 不送出。

現有資料庫的副本可由 `python -m operations.backup_current_state` 建立，放在 `data/backups/`。程式不會因為找不到 `live_execution.sqlite3` 就默默建立一個新的正式交易庫；應先確認原因或從備份還原。備份不包含帳密環境檔。
