# Git 金融帳本交接

使用者明確授權放在原公開程式庫。`snapshots/<id>/account.zip` 是可還原的金融帳本；`manifest.json` 保存 SHA、UTC 時間、資金上限、資料表筆數及 parent；`latest.json` 指向最新交接。公開程式庫任何人可讀取這些帳戶與交易資料。

匯出／驗證／還原使用 `python -X utf8 -m operations.account_transfer export|inspect|restore`，完整操作見 [交易電腦手冊](../TRADING_PC_TEST_RUNBOOK.md)。只允許停止交易後匯出，還原只允許不存在的新目錄；不以 Git 合併 SQLite、不覆寫現有帳本、不修改原歷史。單一帳戶同時只有一台電腦交易；本機鎖不會跨兩台電腦生效。

`.env`、密碼、API keys、憑證、登入資訊、即時行情及各機執行鎖不在快照。新目錄先放入交易電腦原 `.env`、完成綁定與券商只讀對帳，再由使用者啟動 Live。既有實盤授權與資金數值在快照中原樣保存，還原本身不呼叫券商或下單。
