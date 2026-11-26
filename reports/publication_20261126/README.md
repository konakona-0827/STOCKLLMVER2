# 交易電腦移機套件驗證

2026-11-26。交付分支 `codex/production-trading-skill`，以最新 FLOW 基底 `1f3a56265a3440f7aad5ff00b7d67020772058ce` 整併正式 Skill、離線研究、正式 LLM 配對重播及隔離 Paper 程式。交易電腦操作見 [DEPLOY_TRADING_PC.md](../../DEPLOY_TRADING_PC.md)。

## 本次修改

- 正式 Skill 入口沿用原 FLOW 執行器、持倉、對帳、帳戶鎖與資金規則；未修改 `execution/` 或 `config/execution_live.json`。
- 將原研究 checkout 的 `research/llm_paper`、`trading_skills`、`selection_lab`、`multi_horizon`、必要 Skills 與測試納入同一分支，避免依賴未提交的兄弟目錄。
- Paper release export 改從既有帳戶綁定讀取策略選擇，使用此 checkout 的 `codex_cli.py`，補齊最新正式分析器的匯入依賴。增加隔離匯出後子程序匯入測試，確認沒有複製 `.env` 或正式資料庫。
- 修正 Paper 日誌以相同時間與內容生成預設 ID 的衝突；預設事件改用 UUID，明確指定的去重 ID 仍維持去重。既有事件及 hash chain 不重寫。
- Paper 操作入口在新電腦沒有本機部署 registry 時回報明確狀態；不自動重建或覆寫原實驗。
- 增加移機、行情準備、Check／只讀／Live、停止及回退說明；提交後觸發離線 CI。

## 實際驗證

`python -X utf8 -m unittest discover -s tests -v`：**1,100 tests，135.533 秒，OK**。涵蓋原系統回歸、Skill 決策及執行橋接、未來資料洩漏、帳務、Paper 安全、隔離 release、相同時間戳記事件與整個假行情／假模型前向流程。完整輸出見 [full_tests.txt](full_tests.txt)。本次移機驗證沒有呼叫真實模型、登入券商或送單。

提交前檢查暫存內容中的私人檔案、既有 `.env` 實際機密值及 API key 形態，並解析 JSON；沒有發現。原始私人券商快照、憑證、帳本、模型原始紀錄、行情快取及本機部署不納入 Git。公開報告中的本機絕對路徑已移除。套件內容與測試紀錄雜湊見 [manifest.json](manifest.json)。

## 尚待交易電腦驗證

這是工程與移機驗證，沒有新增 11/26 真實市場前向結果。交易電腦須使用原登入環境、綁定其既有帳戶、補齊完成日線並重新取得群益行情及對帳，才由使用者啟動 Live。11/25 報告中的 959 項測試、券商唯讀快照及模型呼叫是當時的驗證，不代表新電腦目前狀態。Paper 歷史結果仍是原版本的開發重播，不與 FLOW 實盤候選混算。

原本機 Paper 程序不因 Git 提交而升級；若要使用本次日誌修正，另建新 immutable release 與隔離實驗，保留舊結果及原部署。遠端 CI 的最終狀態以 GitHub Actions 為準。

## 遠端下載後的修正

首個提交 `919476d` 已從 GitHub 下載驗證。深層 Windows 路徑遇到既有報告的檔名長度限制，手冊改用 `git clone -c core.longpaths=true`，既有 clone 先設定同一選項；補完 checkout 後工作目錄乾淨，Skill SHA 驗證成功。

第一輪遠端 Paper CI 的 Ubuntu 通過，Windows 發現原始碼封存以原始 bytes 計算 SHA、卻以文字讀取轉換 CRLF，造成保存內容與 SHA 不符。新版本只讀取一次 bytes，內容與 SHA 共用同一份快照，保留 CRLF；另增加強制 CRLF／非 ASCII 內容回歸測試。原封存與歷史版本不改寫，後續 CI 仍須全數通過。
