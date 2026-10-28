# develop 整合 Agent／LLM 模式

日期：2026-10-28（Asia/Tokyo）。

## 合併內容

合併前 `develop` 為 `363fe8f`；`origin/codex/agent-develop-parity` 與 `origin/codex/trading-agent-mode` 同為 `913709e`。分支比較為 develop 獨有 0 個、來源獨有 18 個提交，因此以 `git merge --ff-only` 承接完整歷史，沒有衝突。

`Dashboard.cmd` 啟動原有 Dashboard，可選擇 `llm`／`agent`。兩種模式共用既有分析、帳戶、決策驗證、風控與交易執行；Agent SDK 已依 `requirements-agent.txt` 安裝為 0.23.1。未更改本機預設模式或啟動實盤分析。

## 本機資料保留

合併前將 data、config 共 1,479 個檔案保存至父目錄 `.state-backups/develop-before-agent-20261028T145511/`，逐檔比對 SHA256，另保留本機回復分支 `codex/develop-before-agent-20261028T145511`。未複製或輸出 `.env`。

來源分支已移除 SQLite 的 Git 追蹤，並刪除部分舊執行快照。本機將這 587 個被移出版本的既有資料檔從備份還原，保留既有帳本與原始分析證據。SQLite 依專案 `.gitignore` 保持本機資料；保存的舊快照另用 `.git/info/exclude` 忽略，沒有重新提交帳戶資料。

`data/execution/live_execution.sqlite3` 與 `data/analysis/market_history.sqlite3` 的 SHA256 均與合併前相同，`PRAGMA quick_check` 皆為 `ok`。主 checkout 的正式 ROOT 仍為 STOCKLLM。

## 驗證

- Python 3.12.3，235 項 tests 回歸加 3 項 advice interface 測試全部通過，無跳過。
- 測試以臨時 ROOT、空 `.env`、複製的 config／strategies 與測試帳本執行，未連接券商或真實模型。
- 原整合測試的多個 `with` 在 Python 3.12 超過靜態區塊上限，改用 `ExitStack` 管理相同 mocks；兩項策略路徑斷言改比對 resolve 後的路徑，支援 Windows 長／短路徑。
- `python -m pip check` 及 `git diff --check` 通過。
- 確認主 checkout 可解析正式 ROOT 與 v2.2 策略，已安裝 Agents SDK。

此紀錄驗證合併、離線功能及資料保留，沒有重新執行實盤委託或付費 API。另依請求檢查的 `codex/support-desk-phase1` 是獨立歷史的觀察專案，本次未合併該分支。
