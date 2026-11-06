# 2026-10-30 每日參與功能撤回及買單失敗核對

## 已套用

`config/daily_participation.json` 的 enabled 改為 false；正式 Support Scout 策略取消每日次數目標，恢復正常交易理由及獲利門檻。設定每次資格檢查均重讀，因此修改後即停止補試單與買進豁免。停用時也不再於分析結論附加「每日參與尚缺動作」。保留停用的實作及歷史紀錄供追查，不修補後重新啟用此功能、不補送舊失敗單。

自動掃描、正常交易及心跳／分析逾時監督保留。為載入停用提示修正，在核對分析、健康檢查與重新整理皆無活動工作、無待結買賣單後，以既有 Windows 排程重啟 Dashboard。

## 失敗買單：均未送達券商

正式 execution_attempts 顯示四筆 BUY 的 status 均為 SKIPPED_CHECK_FAILED、broker_order_sent=false、crosscheck_performed=false。使用者提到的三筆為陽明，另有較早的一筆長榮。時間以下採日本 UTC+9。

| 時間 | 股票 | 股數 | 建議限價 | 結果 |
| --- | --- | --- | --- | --- |
| 10:40:27 | 長榮 2603 | 1 | 262.5 | refreshed BUY price fails net return, reward/risk or risk budget |
| 11:07:47 | 陽明 2609 | 1 | 62.8 | 同上 |
| 11:38:24 | 陽明 2609 | 1 | 62.8 | 同上 |
| 12:08:51 | 陽明 2609 | 1 | 62.9 | 同上 |

四輪的買進資金預檢皆 PASS，券商 session ready=true，買單未走到送單呼叫。台泥 1101 的一股 SELL 試單另在日本 10:40:27 送出、帳本已記 FILLED，AI 持股由 100 變為 99；此成交不撤銷。

## 原因重現

1. `execution/auto_advice_executor.py` 先呼叫 `store.claim()`，將本筆委託寫成 STARTED。
2. 隨後 `check_refreshed_buy()` 呼叫 `daily_participation.probe_allowed()`。
3. `progress()` 把包含本筆 STARTED 在內的訂單算成未結 BUY，所以 needed 不包含 BUY；本筆試單被自己的防重檢查阻擋，試單豁免變成 false。
4. 程式套回正常 2% 淨報酬與 1.5 報酬風險門檻。按保存行情及當時每側最低費用估算，四單的目標情境淨利依序為 -38、-40.9、-40.8、-40.9 元，均未通過正常獲利門檻；模型失效淨損則約 41.9～43 元，低於當時 50 元模型損失額度。

因此核心原因是試單流程的自我阻擋，非券商拒單、排隊未成交或買進資金不足。原先單元測試直接呼叫送單前重查、未先 claim；漏測完整流程的這個順序。

新增測試在暫存 SQLite 帳本中重現「claim 前豁免成立、claim 後被自身 STARTED 取消並拋出 RiskRejected」。此測試記錄撤回功能的既知缺陷，不代表已修復並重新啟用試單。

## 驗證

- 306 項 tests 回歸及 3 項 advice_interface 測試通過，輸出見同目錄 `daily_participation_withdrawal_20261030_tests.txt`。
- 新增停用測試：即使仍拿著 enabled=true 的舊請求，現行政策 false 仍不補 BUY／SELL 試單、不改寫原決策。既有正常交易測試繼續通過。
- 下一輪請求的 daily_participation.policy.enabled=false，不再插入每日參與目標指令。研究與正常交易的風控不放寬。
- 查核與測試未發出正式委託、未修改歷史成交或持股數量。
