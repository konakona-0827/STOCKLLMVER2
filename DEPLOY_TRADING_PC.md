# 交易電腦取得與啟動

分支：`codex/production-trading-skill`。包含正式 Skill 交易版、研究及 Paper 程式、測試、授權來源與報告。`.env`、憑證、帳戶資料庫、私人的對帳／模型原始記錄、官方行情快取及本機服務註冊不透過 Git 搬移。

本次整併已通過 1,100 項離線測試，修改及限制見 [移機驗證報告](reports/publication_20261126/README.md)。

## 1. 另開程式資料夾

先讓舊 FLOW 正常停止、等待帳戶鎖釋放。保留舊程式及資料作回退；在新的空資料夾取得版本，避免直接切換執行中程式的原始碼。

```powershell
git clone --branch codex/production-trading-skill --single-branch https://github.com/konakona-0827/STOCKLLMVER2.git STOCKLLM-skill
cd STOCKLLM-skill
```

如果這個新資料夾已經存在，從該資料夾更新：

```powershell
git fetch origin
git switch codex/production-trading-skill
git pull --ff-only
```

## 2. 使用原 Python、登入與帳戶資料

最簡單是沿用原交易電腦的 Python 環境、群益 SDK／憑證及同一個 Windows 使用者的 Codex ChatGPT 登入。需要補套件時，在該環境執行：

```powershell
python -m pip install -r requirements-capital.txt -r requirements-agent.txt
```

查明原程式 Check 輸出的 `account_state_root`，再綁定**該台交易電腦既有的帳戶資料目錄**。以下路徑是範例，須改成你的實際目錄：

```powershell
python -m operations.bind_state_root --state-root "D:\Trading\STOCKLLM-account"
```

它只建立本機 `.trading-state.json`，不複製或重設金融資料。原目錄須已有 `.env`、`config/execution_live.json`、`data/execution/live_execution.sqlite3`、`data/analysis/market_history.sqlite3`。不要建立空白帳本來取代它，也不要把密碼或憑證提交 Git。

## 3. 準備完成日線

Git 不含行情快取。你可以沿用交易電腦現有完整行情目錄，以 `-Dataset` 傳入；或在新程式資料夾收集官方資料。2026-11-26 測試前的完成資料日為 11/25：

```powershell
python -m opportunity_flow collect --date 2026-11-25 --output local_data/official_market_20261111
```

程式沿用既有完成日與交易日驗證；其他日期使用當時已完成的資料，不指定未來日線。若來源下載失敗，保留錯誤並修復，不能把缺資料標成已準備好。

## 4. 檢查、只讀、實盤

```powershell
.\SkillTrading.cmd
.\SkillTrading.cmd -Mode Live -ReadOnly
```

第一個只做結構檢查；第二個是只讀畫面。核對帳戶路徑、Skill、完成日線及資金設定，關閉只讀視窗後，再由你啟動真正 Live：

```powershell
.\SkillTrading-Live.cmd
```

若使用現有行情目錄：

```powershell
.\SkillTrading-Live.cmd -Dataset "D:\Trading\market-data"
```

Live 會沿用交易電腦原授權、當前資金、持股及未結單；通過原研究與風控後會實際委託。檢查入口不會為你打開原本停用的執行器。操作與停止方式詳見 [TRADING_SKILL.md](TRADING_SKILL.md)。

## Paper 與研究

實盤版是最新 FLOW／`gpt-6-sol`；先前配對 Paper 是正式 MD／`gpt-5.6-luna`，兩者資料範圍與績效不能混算。研究、重播、隔離 Paper 的 CLI 與設定分別見 [研究套件](research/trading_skills/README.md)及 [Paper 套件](research/llm_paper/README.md)。本機已運行的 Paper 帳本及部署 registry 不會自動搬到交易電腦；新電腦須另建隔離實驗，保留自己的版本及歷史。
