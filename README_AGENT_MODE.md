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

資料來源優先順序：啟動前的 `TRADING_STATE_ROOT` → 本機 `.trading-state.json` 綁定 → 程式所在目錄。設定共用帳戶時，在新版程式目錄執行：

```powershell
python -m operations.bind_state_root --state-root '既有正式帳戶專案的絕對路徑'
```

綁定會核對 `.env`、執行設定及兩個資料庫存在；之後一般 `Dashboard.cmd`、Python 入口和 `RunAgent.ps1` 都讀同一份正式資料及憑證，不需反覆複製。綁定檔只保留於本機，不進 Git；指定來源失效會停止，不會偷偷改讀舊副本。`TRADING_STATE_ROOT` 不能只寫進 `.env`，因為匯入程式時已決定資料目錄。Dashboard 底部顯示實際正式資料路徑，視窗標題顯示程式資料夾。

額外的 Git worktree 必須綁定或明確指定 ROOT，未設定時禁止直接使用隨 Git 帶入的舊資料庫。

SQLite 是持續更新的本機帳戶資料，已移出 Git 追蹤。新電腦需先從備份還原正式資料與 `.env`，再綁定；Git push／pull 不負責帳本同步。

同一正式目錄的 Dashboard 與 BrokerRuntime 各有跨程序作業系統鎖，避免兩個入口同時建立券商工作階段。關閉程式或程序結束會釋放鎖；留下的 `.lock` 檔案本身不代表仍被占用。

**券商庫存更新不等於補回完整帳本。** 對帳只對正式帳本已知的 SEQ13 補核成交／取消；缺少的歷史委託及 AI／人工歸屬不能單憑整戶庫存重建。必須先承接完整帳本，再查券商；詳見 [2026-10-27 對帳修復](reports/state_reconciliation_20261027.md)。

`data/analysis/live_strategy.json` 選用 `strategies/strategy_A_v2_2_calendar_aware_decisions.md`，沿用本地交易檢討的成本與風險規則，修正休市資料判讀並逐一評估可用策略；原 v2、v2.1 完整保留。逐筆證據與限制見 [交易檢討](reports/trade_review_20261025/README.md)。相對路徑由**程式目錄**解析，避免舊電腦的絕對路徑令啟動失敗。需要臨時選擇另一份策略時可設定 `TRADING_STRATEGY_PATH`，路徑不存在會停止分析，不會自行換策略。Dashboard 也可透過原有策略選擇功能更改。使用另一個 `TRADING_STATE_ROOT` 時，會讀取該資料根目錄既有的策略選擇；RunAgent.ps1 明確選用 v2.2。

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

從隔離程式目錄承接既有正式帳戶，可用 `RunAgent.ps1` 明確指定資料根目錄。啟動器核對 `.env`、兩個帳本及執行設定存在，選擇本版 v2.2，並從該資料根目錄載入既有 `.env`。已完成本機綁定後，可省略 `-StateRoot`：

```powershell
pwsh -File .\RunAgent.ps1 -StateRoot '正式帳戶專案的絕對路徑' -ReadOnly
pwsh -File .\RunAgent.ps1
# 進入 Agent Dashboard：移除 -ReadOnly；分析仍依既有實盤設定執行。
```

`--read-only` 會查詢行情、帳戶及模型，寫入分析與健康紀錄，advice 標為 `READ_ONLY`；它不進入正式下單函式。**不帶 `--read-only` 的手動或自動正式分析，會依 `config/execution_live.json` 的啟用設定進入實盤流程。**

離線回歸測試：

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m unittest test_advice_interface
python -m pip check
```

離線測試以替身模擬模型與券商 API，涵蓋 Agent 工具迴圈、預算、BUY／HOLD／SELL、可賣限制、帳本承接、委託去重、行情標的切換及成交對帳。盤中真實 SKCOM／OpenAI 連線和真實委託仍須在使用環境驗證。

已合併原工作分支的交易前檢查：兩種模式共用成本、可驗證價位、冷卻期、風險股數與人工持股保留規則。Agent 的獨立交易前審查同樣經 Agents SDK 使用所選 Agent 模型；審查失敗或否決會撤銷該筆交易。

2026-10-27 修正 Agent 帳戶工具誤把保存快照當原始對帳報告，導致預算與可賣上限歸零的問題。231 項回歸通過，真實 Sol 唯讀分析已核實預算與持股上限；見 [預算修復紀錄](reports/agent_budget_fix_20261027.md)。

2026-10-25 驗證：211 項回歸測試、另 3 項 advice interface 測試及 `pip check` 通過。實際 `gpt-6-sol` SDK 合成只讀測試完成 4 個模型回合、3 個本機唯讀工具，回覆 WAIT 並通過原驗證器；此測試未連接券商或送單，不能代替盤中 SKCOM 驗證。

每輪的 `run.json`、`advice_interface.json`、`execution_report.json` 記錄模式與策略版本；Agent 另寫入 `agent_tools.json`、`agent_sdk_tools.json`、`agent_response.json`。API 使用 `store=False`、停用遠端 tracing，並沿用現有密鑰遮罩。

正式歷史工具會依 run_id／symbol 唯讀查詢已保存的原分析，另提供 `original_decision`（理由、目標、失效價、期限與策略版本）；找不到則為 null，不以委託限價或模型推測補造原計畫。
