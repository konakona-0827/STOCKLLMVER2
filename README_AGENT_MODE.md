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

Agent 與 LLM 共用 `config/web_search.json`；啟用時另外提供 SDK 的唯讀 `web_search`，
沿用相同位置、搜尋範圍、來源紀錄與每輪呼叫上限。上限跨 SDK 多次模型回合累計，
用完後移除搜尋工具，三個本機查詢工具仍可使用。
搜尋接法與每次 Responses 請求的限制依 [OpenAI 網路搜尋說明](https://developers.openai.com/api/docs/guides/tools-web-search)
及 [Responses API 參考](https://developers.openai.com/api/reference/python/resources/responses/methods/create)；
SDK 未回傳可核實的計費次數時，費用保持未知。

Agent 回傳與 LLM 相同的 `market_view` / `decisions` JSON。
原有 `position_analyzer.validate` 檢查格式、持倉、行情、交易計畫與總預算。
通過後交給原有 advice interface、Risk Engine、Python 下單、成交核對及帳務函式。
BUY / SELL 由 Python 執行；HOLD / WAIT 不下單。

`trading_mode=agent` 的額外檢查只比較：Python 算出的本輪 BUY 交易額度
不得超過 Python 確認的剩餘可用額度，並記錄 PASS / BLOCKED。
沿用原有計算中的費用／緩衝，不新增資金政策；不信任 Agent 自算的金額。
數量與 JSON 格式仍由原有驗證器檢查，超預算會阻擋整批委託。
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

2026-10-22：203 項測試通過；另 3 項原有 JSON interface 測試通過，`pip check` 通過。
驗證真正 SDK 多輪工具迴圈、JSON 契約、不合法數字、超時、BUY / SELL / HOLD、
承接既有持倉、Agent 額外預算檢查、送單前資金下降、委託重播與切回 LLM 後持倉保留。
執行測試替換模型／券商外部 API，風控與正式帳務使用原 Python 函式。

## 2026-10-22 與 develop 差異檢查

比較 Agent 分支 `1c56c37` 與另一工作目錄 `STOCKLLM` 的 develop `363fe8f`，
共同祖先為 `edf6478`。已納入 develop 在共同祖先後的 29 個程式、測試、策略及操作文件變更。
日誌、歷史分析、正式交易資料庫、健康快照與策略稽核輸出不作為程式修正複製；
承接正式帳戶時仍用上方的 `TRADING_STATE_ROOT`。

- 保留行情 COM 物件及事件連線，切換查價標的時不再重建回呼；沿用已保存的完整券商成交證據。
- 提供已完成歷史日線、買賣日期及可重建成本，沿用 develop 的可賣股數限制、缺漏標的 WAIT、非交易欄位校正與損益顯示。
- Agent 重新查價時保留日線策略證據；重新查帳戶時更新可賣股數、日期、成本及最終執行預算。
- 初次分析與 Agent 後續查詢共用買進額度計算，保留原本費用預留、買賣價差與每筆金額限制。
- 後續查詢失敗會撤銷該工具之前的成功狀態，阻止採用舊結果交易；工具日誌保留當時快照。
- 補上 Agent 的既有網路查證功能；測試包含真正 SDK／Responses 轉接器，以本機假 HTTP 回應核對序列化與累計上限。
- 修正既有 GUI 測試的 Tk 物件清理，避免後續背景測試觸發錯誤執行緒回收而崩潰。

此檢查只修改 `STOCKLLM-agent`；develop 工作目錄、正式帳戶及交易設定未修改。
驗證未呼叫正式模型、券商或送單；原生 SKCOM 崩潰修正仍需盤中實際切換標的確認。

## 既有唯讀連線驗證

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
