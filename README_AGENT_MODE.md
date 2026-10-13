# Trading Agent Mode MVP

分支：`codex/trading-agent-mode`。程式在隔離工作目錄
`C:\Users\ho.chouchuan\Documents\HO\project\personalmono\STOCKLLM-agent`。
原專案的程式與交易設定未修改。

只替換 LLM 的決策段：

```text
原有排程／分析按鈕 → TRADING_MODE → LLM 或 Agent
                              → 相同決策 JSON
                              → 原有風控、Python 下單、成交核對與帳務
```

兩模式共用目前資料庫、資金上限與正式 AI 持倉，不建立新的模擬帳戶，
不改動 Experiment Versioning，也不另建排程器。
既有 `config/execution_live.json` 已啟用實盤執行；兩模式依同一設定執行。

## 啟動並承接目前帳戶

```powershell
cd C:\Users\ho.chouchuan\Documents\HO\project\personalmono\STOCKLLM-agent
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-agent.txt
```

啟用時由一個 GUI／既有排程負責執行，先停止舊的執行入口。
從開發分支讀取原專案最新設定與資料庫：

```powershell
$env:TRADING_STATE_ROOT='C:\Users\ho.chouchuan\Documents\HO\project\personalmono\STOCKLLM'
$env:TRADING_MODE='agent'
.\.venv\Scripts\python.exe dashboard.py
```

`TRADING_STATE_ROOT` 必須在啟動前設定，只選擇設定與資料來源。
程式仍從目前分支載入，不複製或重置資金與持倉。
省略時沿用程式所在專案的資料目錄。

分析按鈕旁增加 `llm` / `agent` 下拉選擇器，按下分析時依選擇分流。
執行中禁止變更模式，結束後恢復；畫面顯示當前模式與策略。
自動掃描沿用同一選擇與原有 30 分鐘 Scheduler。
切回 LLM 可在 GUI 選 `llm`，或啟動前設定 `$env:TRADING_MODE='llm'`。

也可在實際資料來源的 `.env` 設定：

```ini
TRADING_MODE=llm
# 或 TRADING_MODE=agent
# Agent 預設沿用 OPENAI_MODEL，以下皆可省略
OPENAI_AGENT_MODEL=gpt-5.6-luna
AGENT_MAX_TURNS=24
AGENT_TIMEOUT_SECONDS=180
```

沿用環境變數優先於 `.env` 的行為；修改 `.env` 後重啟程式。
沒有設定時預設 LLM，無效模式會停止並報錯。

手動執行當前模式一輪：

```powershell
.\.venv\Scripts\python.exe trading_router.py --mode agent
```

既有 Windows Task Scheduler 可使用相同入口並加 `--trigger AUTO`。
沿用交易時段、GUI 冷卻、執行鎖與原有委託去重。

## 分工與紀錄

重用 `position_analyzer.build_request` 的輸入與原有 JSON 欄位。
單一 OpenAI Agents SDK Agent 可多次呼叫三個唯讀工具：
`get_market_data(symbol)`、`get_portfolio()`、`get_trade_history()`。
資料來自 SKCOM、目前券商核對及正式帳本。不提供 Shell、Skill、其他 Agent 或下單工具。
SDK 接法依 [OpenAI 官方快速入門](https://developers.openai.com/api/docs/guides/agents/quickstart?lang=python)。

Agent 回傳與 LLM 相同的 `market_view` / `decisions` JSON。
原有 `position_analyzer.validate` 檢查格式、持倉、行情、交易計畫與總預算。
通過後交給原有 advice interface、Risk Engine、Python 下單、成交核對及帳務函式。
BUY / SELL 由 Python 執行；HOLD / WAIT 不下單。

`trading_mode=agent` 讓原 Python 執行器額外重算原始 JSON 的 BUY 股數、
可驗證 ask、費用／緩衝與合計金額，再與 Python 確認的可用預算比較。
不信任 Agent 自算金額；不合法數量或超預算會阻擋整批委託。
原有送單前券商資金、持倉與其他風控保持啟用。

模式記錄在 run manifest、advice、execution report 與委託明細，
策略原文、SHA256 與原有版本機制保留。
同一回合目錄追加 `agent_tools.json`、`agent_sdk_tools.json`、`agent_response.json`，
記錄工具參數／結果、版本、Token 與錯誤。
遠端 tracing 關閉，API 使用 `store=False`，沿用密鑰遮罩。
SDK 錯誤、超時或回合上限會產生 WAIT，不送單。

## 驗證

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.\.venv\Scripts\python.exe -m unittest test_advice_interface
.\.venv\Scripts\python.exe -m pip check
```

原版 172 項加新增 7 項，共 179 項通過；另 3 項原有 JSON interface 測試通過。
驗證真正 SDK 多輪工具迴圈、JSON 契約、不合法數字、超時、BUY / SELL / HOLD、
承接既有持倉、Agent 額外預算檢查、送單前資金下降、委託重播與切回 LLM 後持倉保留。
執行測試替換模型／券商外部 API，風控與正式帳務使用原 Python 函式。

使用目前帳戶但禁止下單的連線驗證：

```powershell
.\.venv\Scripts\python.exe trading_router.py --mode agent --read-only
```

只讀驗證更新正常分析與健康紀錄；advice 標示 `READ_ONLY`，防止其他入口重播。
2026-10-13 日本時間 20:59 的真實 OpenAI／SKCOM 驗證：6 次模型回合、
5 次工具呼叫，回傳符合原契約的三筆 HOLD，驗證 VALID。
2891／30 股、1101／100 股、2603／10 股保留，正式委託紀錄仍為 11 筆，沒有送單。
紀錄在原專案 `data/analysis/runs/20261013T195727-gui-dc1fa028/`。
盤中實際買賣尚未送出；開市後可由同一入口與原有風控執行。
