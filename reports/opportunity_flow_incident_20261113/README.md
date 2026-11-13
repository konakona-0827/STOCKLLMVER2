# 2026-11-13 Opportunity Flow 執行錯誤

台灣時間 2026-11-13 10:52:18 取證。此分支以事故當下的 Git 版本 `d740a35b5a772423ca309c92158484ec0942ff79` 為父，保留當時使用的本機啟停介面變更及測試。

舊版視窗狀態與內建紀錄皆顯示以下錯誤。模組的本機絕對路徑已省略。

```text
ImportError: cannot import name 'pending_exit' from 'position_manager'
```

最後的啟動日誌顯示 08:30 已開始公開行情輪詢，且 08:30:12 券商核對完成，之後有零股行情連線紀錄。錯誤文字由介面取得，發生秒數沒有記錄。FLOW 帳本當時有 0 輪掃描結果、0 次模型呼叫及 0 筆委託交接。

`opportunity_flow/legacy_runtime.py` 的 `candidates()` 從 `position_manager` 匯入 `pending_exit`。函式定義在 `execution/exit_cancel.py`。新版遠端提交 `a57635f28fcec2f766cde3fbcfce7fd01c4f2dbc` 亦有相同匯入。此處保留原程式以便追查。

[incident.json](incident.json) 保存版本、介面錯誤、當日資料來源事件和執行日誌片段。
