# 正式 FLOW 自動執行確認

2026-11-13；延續 `d7e656f`。本次核對自動啟動與原交易通道，沒有啟動正式交易程序或送出實盤委託。

**使用者雙擊此分支的 `OpportunityFlow.cmd` 後，介面會自動啟動 FLOW；通過決策與風控的 BUY／SELL 自動交給原執行器，不需逐筆確認，也不需另外呼叫買單 Python。** 原六檔報告的「使用者自行操作」已改寫，避免把聊天室沒有代送測試單，誤解成日常交易必須人工操作。

## 已核對的正式路徑

1. `OpportunityFlow.cmd` 無參數時指定 `RunOpportunityFlow.ps1 -Mode Live`。
2. `live` 預設沒有 `--read-only`，進入完整 `FlowDashboard`；介面透過 `after(0, start_auto)` 自動啟動。
3. `FlowController` 啟動 `watch --live --llm --public-quotes --refresh-daily`，使用綁定的原正式帳戶、帳本與券商設定。純 CLI `live --headless` 也啟用同樣的自動研究與執行參數。
4. 研究結果由 `LiveCoordinator.offer()` 自動排入 `ExecutionBridge.step()`；買進先核對兩方批准的同一固定計畫、最新帳戶、價格、股數、公司事件與下單前複核。
5. 通過後自動走 `LegacyRuntime.execute()` → 原 `BrokerRuntime.execute_if_active()` → 原 `execute_after_advice_written()` → 原 `execute_advice_file()` → 原 `CapitalOddLotExecutor.send_limit_order()`。這條路徑沒有 `input()` 或逐筆批准對話框。
6. 原執行器保留送單前帳戶／行情／風控檢查、正式帳本去重與券商回報核對；只按確認成交量回寫 AI 股數。受理、部分成交、未確認及拒單各自記錄。
7. 既有 AI 持股約每分鐘進入管理檢查，依個別計畫與新行情重審、複核並自動賣出；沒有新買進候選及買進額度為零，也不會因此關閉退出管理。已送出的未結單照原券商核對與撤單流程處理，不盲目重送。

人工股數不會自行變成可賣的 AI 股數。既有人工作業持股若要交給模型，先在「持股歸屬／轉交 AI」設定一次管理股數及必要成本；這是持股歸屬設定，不是逐筆買賣批准。AI 買進的確認成交股數會自動納入正式 AI 帳本。

## 啟動方式與時點

下週使用本分支的 **`OpportunityFlow.cmd`**，開盤前即可先開啟放置；不用再按一次「啟動 FLOW」。如果之前手動按過「停止 FLOW」，才需要再按啟動。

交易日台灣 08:30 起進行研究，允許正式委託的時間為台灣 09:00～13:30，日本時間分別是 09:30 起研究、10:00～14:30 允許委託。交易日另由程式日曆檢查。原先的一般小時掃描與舊持倉排程不會另外啟動，避免重複執行。

也可明確使用：

```powershell
# 正式完整介面，自動啟動與執行
.\RunOpportunityFlow.ps1 -Mode Live

# 正式純 CLI，自動啟動與執行
.\RunOpportunityFlow.ps1 -Mode Live -Headless

# 只有明確加上 ReadOnly 才是不送單的只讀模式
.\RunOpportunityFlow.ps1 -Mode Live -ReadOnly
```

`RunOpportunityFlow.ps1` 未指定模式時仍是 Demo；日常請使用上述正式入口。同帳戶舊程式須先關閉，電腦保持清醒，券商憑證、Codex CLI 登入及網路有效。券商讀取逾時會阻止新動作並要求重新啟動；這版不承諾連線故障後永遠無須人工處理，也不保證市場一定出現可成交機會。

## 本次證據

- 啟動器 `-Mode Check`：綁定原正式帳本、執行器 enabled、相依套件 ready；此檢查沒有登入券商或送單。
- 現行正式設定：最低保留 NT$5,000、每筆最低佣金 NT$1、每筆資金預留 NT$20；沒有改動逐筆批准機制，因為正式流程本來就沒有這一步。
- 新增三項測試：預設 GUI 自動啟動、GUI controller 啟用正式研究／原執行器、Headless 正式參數與明確只讀參數的傳遞。
- 本次相關測試 **60 項通過**：完整 Dashboard 20 項、執行橋接 40 項。原 `d7e656f` 的全套 700 項紀錄保留，沒有把本次局部測試寫成新的全套結果。
- 既有正向橋接案例實際執行原送單 Python 程式，只替換 SDK 邊界，檢查固定股數、確認部分成交才回寫及防重送。這是程式串接證據；真實受理／成交仍需正式運行後的券商證據，不能用測試代替。

本次只有文檔澄清與三項回歸測試，沒有重建或替換原執行器，也沒有透過實盤交易驗證。前一輪真實 CLI／券商只讀與逐股研究結果見 [六檔實查報告](../six_candidate_cli_20261113/README.md)。
