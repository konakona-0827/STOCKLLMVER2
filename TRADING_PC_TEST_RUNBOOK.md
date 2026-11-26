# 新 Skill：交易電腦完整測試流程

分支：`codex/production-trading-skill`。這份流程用新 Skill 接入最新 FLOW 的正式 LLM、群益執行器及原帳本，測試決策、實際委託、成交與後續持倉管理。步驟 1～6 完成準備及禁止送單的核對；步驟 7 由你明確啟動真正 Live。

密碼、群益憑證、SDK 安裝與 Codex ChatGPT 登入沿用原分支的方式。帳本依使用者指示放在**同一公開 Git 程式庫**的 `account-share/`，其中可能有券商帳號、持股與成交明細，任何人可下載；`.env`、私鑰與憑證檔不提交。Git 不提供兩台電腦間的即時交易鎖，**同一帳戶同時只啟動一台交易電腦**。

## 1. 停止原程式，再取得新分支

先在目前交易電腦按「停止 FLOW」，等工作結束並正常關閉舊視窗。不要靠直接結束所有 Python 程序來交接；已送券商的委託仍可能成交，須保留帳本並重新對帳。

以下在 PowerShell 執行。`D:\Trading` 為範例工作目錄，`$OriginalState` 改成**交易電腦原本含 `.env`、`config`、`data` 的帳戶目錄**，不是開發機路徑。

```powershell
$WorkParent = 'D:\Trading'
$OriginalState = 'D:\Trading\STOCKLLM'
Set-Location $WorkParent
git clone -c core.longpaths=true --branch codex/production-trading-skill --single-branch https://github.com/konakona-0827/STOCKLLMVER2.git STOCKLLM-skill
if ($LASTEXITCODE -ne 0) { throw '取得程式失敗，先排除 Git 錯誤。' }
Set-Location .\STOCKLLM-skill
git log -1 --oneline
git status --short --branch
```

若已取得這個新資料夾，停止程式後在該資料夾更新：

```powershell
git config core.longpaths true
git fetch origin
git switch codex/production-trading-skill
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw '更新未完成，不能帶著衝突開始交易。' }
```

遇到本機修改先保留並檢查，不使用 `reset --hard`、不合併兩份 SQLite、不 force push。長路徑設定處理原報告路徑超過 Windows 限制的問題。

## 2. 決定使用哪份金融帳本

先查看 Git 快照，這一步不登入券商、不改原帳本：

```powershell
python -X utf8 -m operations.account_transfer inspect
if ($LASTEXITCODE -ne 0) { throw 'Git 帳本驗證失敗，停止交接。' }
```

核對 `snapshot_id`、`created_at`、`parent_snapshot_id`、兩個資料庫各表筆數及資金上限。快照時間是 UTC；日本時間加 9 小時，台灣時間加 8 小時。Git 快照不是當前券商狀態，也不包含即時行情。

### A. 交易電腦原本已有較新帳本：直接沿用

如果交易電腦原帳本在 Git 快照之後已有成交、委託、資金或持股變動，原帳本就是本次權威來源。不要用舊快照覆蓋它：

```powershell
$AccountState = $OriginalState
python -X utf8 -m operations.bind_state_root --state-root "$AccountState"
if ($LASTEXITCODE -ne 0) { throw '帳戶綁定失敗。' }
```

這條路不需要還原 Git。若之後要將這份不同來源的帳本接上 Git 交接鏈，先核對兩份紀錄；工具出現 `STALE_ACCOUNT_HANDOFF_PULL_AND_COMPARE` 時不刪 receipt、不修改 latest、不強行覆蓋。它表示來源尚未承接目前快照，需先解決資料分歧。

### B. 使用 Git 帳本交接到這台電腦：還原至新目錄

只有確認 Git 快照是要接續的完整帳本，且來源電腦已停用交易時，才走這條路。目標目錄**必須不存在**，原帳戶目錄保留不動：

```powershell
$ImportedState = 'D:\Trading\STOCKLLM-account-import-20261126'
python -X utf8 -m operations.account_transfer restore --target "$ImportedState"
if ($LASTEXITCODE -ne 0) { throw '帳本未還原完成，不要繼續。' }
Copy-Item -LiteralPath (Join-Path $OriginalState '.env') -Destination (Join-Path $ImportedState '.env')
$AccountState = $ImportedState
python -X utf8 -m operations.bind_state_root --state-root "$AccountState"
if ($LASTEXITCODE -ne 0) { throw '帳戶綁定失敗。' }
```

`.env` 使用這台交易電腦既有版本，憑證維持原 Windows 憑證／檔案設定，SDK 路徑保持這台電腦可用的值。新機完全沒有原登入環境時，先按原券商安裝方式準備；Git 沒有密碼或憑證可代替這一步。還原不綁定、不登入、不送單，不合併或取代已有帳戶。

若原 PowerShell 曾設定 `TRADING_STATE_ROOT`，它會優先於綁定檔；這個視窗要使用剛選的同一目錄：

```powershell
$env:TRADING_STATE_ROOT = $AccountState
```

同時核對 Windows 使用者／系統環境值，避免稍後雙擊入口讀到另一個帳戶。Check 輸出的 `account_state_root` 必須正確。

## 3. Python、SDK 與 LLM 登入

使用原交易電腦已成功使用群益的 Python 與同一位 Windows 使用者；沿用原 SDK 位元架構、COM 註冊與憑證。需要補依賴時：

```powershell
python --version
python -m pip install -r requirements-capital.txt -r requirements-agent.txt
if ($LASTEXITCODE -ne 0) { throw '套件安裝失敗。' }
```

Python 3.12 是本分支驗證環境。下列只查 Codex CLI 登入狀態，不呼叫交易模型、不下單：

```powershell
@'
import subprocess
from codex_cli import executable, child_environment
raise SystemExit(subprocess.call([executable(), 'login', 'status'], env=child_environment()))
'@ | python -X utf8 -
```

預期顯示使用 ChatGPT 登入。`CODEX_CLI_NOT_FOUND` 表示先修正原 CLI 安裝或 `.env` 的 `CODEX_CLI_PATH`；登入過期則沿用原登入流程。API-key 登入會被正式傳輸拒絕，不以另一套付費 API 代替失敗。

## 4. 補齊官方完成日線，再取得群益即時零股行情

這是兩種不同資料：官方完成日線用於策略結構；當前買賣價與可成交依據由群益取得。Git 快照的 `market_history.sqlite3` 不是 FLOW 全市場日線資料集。

2026-11-26 測試時，已完成資料日填 `2026-11-25`；其他測試日改成當時已完成的最新交易日。不要填未完成的當日日線：

```powershell
$CompletedDay = '2026-11-25'
$DatasetPath = Join-Path (Get-Location) 'local_data\official_market_20261111'
python -X utf8 -m opportunity_flow collect --date $CompletedDay --sessions 65 --output "$DatasetPath"
if ($LASTEXITCODE -ne 0) { throw '官方日線收集失敗，不能開始 Live。' }
Get-Content -LiteralPath (Join-Path $DatasetPath 'manifest.json') -Raw | ConvertFrom-Json | Select-Object calendar_verified_through,start,end
```

`calendar_verified_through` 應覆蓋指定完成日；逐檔仍須通過完整性與有效交易日檢查，不能只改 manifest 日期。可沿用原電腦現有的完整 FLOW 資料集，但後面各入口必須傳入同一個 `$DatasetPath`。

群益即時行情在下一步的只讀畫面自動登入、訂閱與取得，不需要另外開送單測試。驗收欄位如下：

| 檢查 | 預期 |
|---|---|
| 交易所／商品 | 股票代碼與上市、上櫃一致；市場 5／6 為對應零股行情 |
| 報價來源 | `quote_basis=capital_intraday_odd_lot`，`quote_status=LIVE` |
| 試撮與時點 | `is_trial=false`；同一台灣交易日，時間不在未來；交易前報價年齡不超過目前 120 秒門檻 |
| 買賣價 | bid／ask／last 為有效價格、bid ≤ ask；盤中高低與原計畫一致 |
| 帳戶 | 最新券商對帳已完成、持股核實、未結委託及資金預留已反映 |

低成交量股票的最後成交可能較舊；五檔收到新回呼不表示最後成交一定新鮮，查看保存的來源與時間。盤外或缺行情時，保持缺資料／阻擋狀態；Yahoo 非官方價、公開整股參考價、歷史快取都不能充當可用零股成交價。

## 5. Check：核對 Skill、帳戶與最新資金規則

```powershell
.\SkillTrading.cmd -Dataset "$DatasetPath"
```

預期：Skill `tw-production-decision`、version 1、enabled true、模型 `gpt-6-sol`，Skill SHA 與 `config/trading_skill.json` 一致，`structural_ready=true`，帳戶目錄正確。`session_trade_ready=false` 是預期值，因 Check 不做盤中券商驗收；不能手動把它改成 true。若 `executor_enabled=false`，先處理原有授權／停用原因，不為這次測試直接打開旗標。

資金以**綁定帳戶**的設定及資料庫為準，不用程式資料夾內的範例值覆寫。這次來源快照沿用的最新規則：

| 項目 | 值 |
|---|---:|
| AI 累計投入上限 | NT$10,000 |
| 券商可用額最低保留 | NT$5,000 |
| 單日買進上限 | NT$30,000 |
| 單筆價金／股數上限 | NT$100,000／999 股，另受剩餘額度限制 |
| 買單現金預留 | 價金 + max（價金 1%，NT$20） |
| 估計佣金 | 0.1425%，每筆最低 NT$1；實收依帳戶對帳 |
| 預掛模式 | 本次來源 `planned_limits_enabled` 未啟用，沿用原即時對手價路徑 |

NT$10,000 是累計上限，已有持股與未結預留會扣除，不能因換機再多投入一份。新買額度取券商可用額扣保留／未結、AI 剩餘額度、當日剩餘額度的最小值。NT$20 是預留，不能全當實收佣金扣績效。原配置、費後與風險門檻維持原值；不要為了看到成交降低門檻。

## 6. 先跑只讀，完成帳戶與資料驗收

```powershell
.\SkillTrading.cmd -Mode Live -ReadOnly -Dataset "$DatasetPath"
```

這個畫面不送出券商委託，仍可能寫入對帳／研究紀錄並呼叫 LLM、消耗既有方案額度。檢查帳戶、AI 與人工持股歸屬、原未結委託、當日買額及新買可用額；與群益原介面比對。遇到差異、未核實餘額或不明委託，先在原流程核對，不歸零或刪帳本。

看「FLOW 全母體與研究卡」「研究判斷清單」及健康頁，確認日線更新、群益行情與阻擋原因。健康檢查若詢問固定送單測試，本次策略能力驗收選擇**不加入**；固定診斷委託不算 Skill 的決策。核對完成後正常關閉只讀視窗，等它退出再開始下一步。

## 7. 啟動真正 Live，逐層確認實際決策與操作

**以下命令會啟動真正交易，通過原條件後可自動送出群益委託。** 確認另一台電腦與舊程式都已停止，同一帳戶只由這台管理：

```powershell
.\SkillTrading-Live.cmd -Dataset "$DatasetPath"
```

畫面標題應為「TW Trading Skill｜實盤」。入口沿用原 supervisor、恢復程序、帳戶鎖及實盤授權。若顯示正在執行就等待；若顯示人工暫停，再由你按「啟動 FLOW」。有效市場時段由原系統判斷，盤外等待不算故障。

按順序驗收一檔股票，不以程式開啟或出現 BUY 字樣就認定完成：

1. **LLM 研究**：雙擊完整研究卡，核對凍結輸入、計畫、researcher／challenger 兩方意見、模型與時間。保存內容帶 `trading_skill` 身分；模型成功回覆與 WAIT／WATCH／REJECT、模型錯誤分開。CLI 用量有紀錄；缺 token 保持未知。兩方研究可能來自同條件快取，不要求每次刷新都再呼叫模型。
2. **進場批准**：雙方研究通過後，重新取得群益行情、券商資金與帳本；下單前獨立審查有效且批准。Skill 變更、舊批准、行情過期或原計畫失效會被擋下。
3. **實際委託**：檢查正式執行結果的 `broker_order_sent`、委託狀態與券商序號；到群益原介面核對同代碼、買賣別、盤中零股、股數與價格。只有模型建議或 handoff 檔不代表已送單。
4. **成交與帳務**：券商受理不代表成交。比對券商實際成交時間／價格／數量、未成交餘額及 AI 持股回寫；部分成交只增加核實股數。重新整理或恢復流程不得重送同一 decision。
5. **持倉與退出**：持續開啟程式觀察 AI 持倉、可賣數量、退出研究及正式賣出。原人工部位未轉交 AI 前仍按原歸屬處理。當日新買零股的完整退出驗收依原可賣檢查跨交易日進行，不拿不能合法賣出的當日高價充作獲利。

沒有合格交易時保存原原因，不能為製造測試成交強制 BUY。`UNCONFIRMED`／`DISPATCH_UNKNOWN_DO_NOT_RETRY` 或無法核實券商受理時，先停止新工作並核對原委託，不手動重送同一意圖。

## 8. 收集測試證據與績效

```powershell
python -X utf8 -m opportunity_flow status --state local_data/production_skill_live
Get-ChildItem .\local_data\production_skill_live\handoffs
```

| 證據 | 位置／內容 |
|---|---|
| Skill、研究、模型使用量 | 新程式的 `local_data/production_skill_live/flow.sqlite3`、`cycles/`、`packets/`；`trading_skill_profiles` 保留內容與 SHA |
| 建議與正式執行 | `local_data/production_skill_live/handoffs/` 的 payload 與 `.execution.json` |
| 券商與金融帳本 | 綁定帳戶的 `data/execution/live_execution.sqlite3`、`data/health/latest_health.json` |
| AI 額度與原歷史 | 綁定帳戶的 `data/analysis/market_history.sqlite3` |

每筆記錄：測試日、Git commit、Skill SHA、股票、決策時間與理由、報價時間／年齡、雙方意見、pretrade 結果、送單狀態、券商序號、成交量價、成本、實現／未實現損益與錯誤。先扣手續費、稅及實際可觀測成本，再看盈利；LLM confidence 不填成勝率。

舊正式 MD／`gpt-5.6-luna` Paper 與這個 FLOW／`gpt-6-sol` Live 是不同實驗，不能混算為同條件 Baseline。實盤先測新 Skill；若要前向同條件 A/B，兩組應另外使用相同行情、模型、資金、成本與模擬成交的隔離 Paper，不能讓兩個 Live 策略同時操作同一帳戶。

## 9. 收工、Git 交接與回退

先按「停止 FLOW」，等待工作結束，正常關閉視窗；不要只關啟動命令列，supervisor 可能仍在。核對券商未結委託，停止程式不等於撤單。移轉工具遇到 OS lease 或資料庫寫入中會拒絕匯出，先等原 owner 退出，不刪 lock 檔繞過。

在**剛完成交易、持有最新帳本的那台電腦**，先取得同一分支最新內容，再建立新快照：

```powershell
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw 'Git 未同步，先處理分歧。' }
python -X utf8 -m operations.account_transfer export --state-root "$AccountState"
if ($LASTEXITCODE -ne 0) { throw '帳本交接失敗，先保留原資料。' }
python -X utf8 -m operations.account_transfer inspect
if ($LASTEXITCODE -ne 0) { throw '快照核對失敗。' }
git add -- account-share
git diff --cached --stat
git commit -m 'Record account handoff after trading test'
git push origin codex/production-trading-skill
if ($LASTEXITCODE -ne 0) { throw '尚未分享成功，另一台先不要接手交易。' }
```

匯出保存兩個金融 SQLite、既有 live 設定／候選設定及可選策略選擇檔，不包含 `.env`、憑證、即時行情、owner／自動恢復狀態、LLM 原始記錄或各電腦的 FLOW cache。SQLite backup 會包含已提交的 WAL；另檢查資料庫完整性、檔案白名單、SHA 及已知密碼／API key，保留舊快照並記錄 parent。Git push 失敗不重做交易、不 force push。

下一台 `git pull --ff-only` 後 inspect，按步驟 2B 還原至另一個新目錄、放入它原本的 `.env`、綁定，再從只讀對帳開始。receipt 不同而匯出遭拒表示帳本分歧，不能用 Git 二進位合併解決。Git 只負責快照交接；另一台在交接後不得繼續用舊帳本交易。

回退程式時停止新 Skill，等待 owner 釋放，再啟動原版本並綁定**目前最新金融帳本**、重新對帳。保留已發生的委託、成交與歷史，不能為回退程式還原舊金融快照。研究／Skill 改版在停止交易後建立版本與驗證。

## 常見阻擋

| 狀態 | 處理 |
|---|---|
| 帳戶路徑錯／必要檔案缺失 | 重做綁定、核對 `TRADING_STATE_ROOT`，不初始化空帳本 |
| 日線缺漏／日期未完成 | 修復官方下載或指定正確資料集，不改日期騙過檢查 |
| 行情 stale／試撮／非零股 | 等有效群益回呼，查市場別、SDK 連線與時鐘；不放寬門檻 |
| 模型登入／額度／逾時 | 保留錯誤，修復原 CLI；不算正常 WAIT、不改付費 API |
| 買進額度為零 | 核對既有投入、保留與未結；不重置上限或清除帳本 |
| 委託不明／庫存不符 | 停新工作、查券商與原對帳；不重送、不把未知算成交 |
| `RESTORE_TARGET_MUST_NOT_EXIST` | 指定全新目錄，保留原帳本 |
| `STALE_ACCOUNT_HANDOFF_PULL_AND_COMPARE` | 先 pull 並核對帳本來源；不刪 receipt 或改 latest 強行接續 |
