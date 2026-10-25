# AI Agent 決策模式

這個版本整合 `develop` 的分析資料、正式交易限制、券商連線與對帳、Dashboard 顯示及策略更新。`llm` 和 `agent` 只在決策推論處分流；兩者輸出相同的 `market_view` / `decisions` 契約，之後共用 Python 驗證、風控、送單、成交核對與正式帳本。

```text
原有排程／分析按鈕 → 模式選擇 → LLM 或 Agent 決策
                               → 相同決策 JSON
                               → 相同驗證、券商風控、送單及對帳
```

Agent 使用 OpenAI Agents SDK，提供 `get_market_data(symbol)`、`get_portfolio()`、`get_trade_history()` 三個本機唯讀工具，並依共同的 `config/web_search.json` 提供有每輪累計上限的網路查證。模型不持有下單工具；交易仍由現有 Python 執行器負責。Agent 的預設模型為 [GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol)（`gpt-6-sol`），可用 `OPENAI_AGENT_MODEL` 明確覆寫。LLM 模式繼續使用 `OPENAI_MODEL`。Agent API 失敗、逾時或格式未通過驗證時，本輪不送單。

## 安裝及資料來源

在本版本程式目錄執行：

```powershell
python -m pip install -r requirements.txt -r requirements-agent.txt
python -m pip check
```

未設定 `TRADING_STATE_ROOT` 時，程式使用所在目錄的 `config/`、`data/` 和 `.env`。若要從另一個 checkout 承接**同一**正式帳本，請在啟動 Python 前將 `TRADING_STATE_ROOT` 設為該 checkout 的絕對路徑；此變數不能只寫在 `.env`，因為程式匯入時就會決定資料根目錄。同一時間只能由一個 Dashboard／排程入口操作該資料根目錄。

`data/analysis/live_strategy.json` 預設選用 `strategies/strategy_A_v2_adaptive_opportunity.md`，內容與 `develop` 當時選用的策略檔相同；相對路徑由**程式目錄**解析，避免舊電腦的絕對路徑令啟動失敗。需要臨時選擇另一份策略時可設定 `TRADING_STRATEGY_PATH`，路徑不存在會停止分析，不會自行換策略。Dashboard 也可透過原有策略選擇功能更改。

## 模式與測試

Dashboard 的 `llm`／`agent` 下拉選擇器會顯示當前模式，分析執行中禁止切換。自動掃描沿用現有 Scheduler、券商 COM 執行緒及執行鎖。`.env` 可設定：

```ini
TRADING_MODE=llm
OPENAI_MODEL=gpt-5.6-luna
OPENAI_AGENT_MODEL=gpt-6-sol
AGENT_MAX_TURNS=24
AGENT_TIMEOUT_SECONDS=180
```

先確認正式策略可載入，再跑不送單的 Agent 連線驗證：

```powershell
python -c "import strategy_catalog; print(strategy_catalog.live_selection()['path'])"
python trading_router.py --mode agent --read-only
```

`--read-only` 會查詢行情、帳戶及模型，寫入分析與健康紀錄，advice 標為 `READ_ONLY`；它不進入正式下單函式。**不帶 `--read-only` 的手動或自動正式分析，會依 `config/execution_live.json` 的啟用設定進入實盤流程。**

離線回歸測試：

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m unittest test_advice_interface
python -m pip check
```

離線測試以替身模擬模型與券商 API，涵蓋 Agent 工具迴圈、預算、BUY／HOLD／SELL、可賣限制、帳本承接、委託去重、行情標的切換及成交對帳。盤中真實 SKCOM／OpenAI 連線和真實委託仍須在使用環境驗證。

2026-10-25 驗證：209 項回歸測試、另 3 項 advice interface 測試及 `pip check` 通過。實際 `gpt-6-sol` SDK 合成只讀測試完成 4 個模型回合、3 個本機唯讀工具，回覆 WAIT 並通過原驗證器；此測試未連接券商或送單，不能代替盤中 SKCOM 驗證。

每輪的 `run.json`、`advice_interface.json`、`execution_report.json` 記錄模式與策略版本；Agent 另寫入 `agent_tools.json`、`agent_sdk_tools.json`、`agent_response.json`。API 使用 `store=False`、停用遠端 tracing，並沿用現有密鑰遮罩。
