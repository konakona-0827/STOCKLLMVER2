# PLANNEDLIMIT 的 Codex CLI 推論

分支 `codex/plannedlimit-codex-cli` 以 `codex/planned-limit-orders@6636cf3` 為基底。
推論走本機 Codex CLI 的 ChatGPT 登入，使用方案的 Codex 利用額度；仍是連線雲端推論。
API Key 登入、未登入、CLI 失敗或額度不足都會停止該次推論，沒有 API 備援。
主決策失敗維持全部 WAIT；子研究／複核失敗沿用既有拒絕交易的檢查。

## 準備與啟動

需要 Python 3.11+、Agents SDK 與支援 `exec --ignore-user-config --output-schema` 的原生 Codex CLI。
本機已用 Codex CLI `0.166.0-alpha.6` 驗證。CLI 從 PATH 或 Codex desktop 安裝目錄自動尋找；
也可用 `CODEX_CLI_PATH` 指向原生執行檔。Windows 的 npm `.cmd` 包裝請改指定其原生 `codex.exe`。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-agent.txt
codex login
codex login status  # 應顯示 Logged in using ChatGPT
```

Git worktree 必須沿用既有帳戶狀態目錄。這次建立的本機工作目錄已綁定 `../STOCKLLM`；
綁定檔 `.trading-state.json` 不會提交。其他機器請用 `-StateRoot` 指定原正式資料目錄。
既有 `.env` 的 `OPENAI_API_KEY` 不會傳給 CLI，也不參與此分支推論。

```powershell
# 先核對本地登入／帳本／依賴；不呼叫模型或券商
.\.venv\Scripts\python.exe -m support_scout doctor
# 唯讀跑一輪：連券商行情與 CLI，禁止送單
.\RunCodexAgent.ps1 -StateRoot ..\STOCKLLM -ReadOnly
# 開啟既有 Dashboard，沿用帳戶的交易授權設定
.\RunCodexAgent.ps1 -StateRoot ..\STOCKLLM
```

已有 Python 環境時可加 `-PythonPath <python.exe 的路徑>`。入口不啟用自動掃描。
正式限價功能的啟用設定、帳本及風控沿用原程式；此次沒有啟動正式交易或修改交易授權。

`CODEX_MODEL` 可覆寫 CLI 模型；未設定時 Agent 沿用 `OPENAI_AGENT_MODEL`，預設 `gpt-6-sol`。
舊版 LLM 模式沿用其 `OPENAI_MODEL`；若該模型在 Codex 不可用會回報失敗，請明確設定 `CODEX_MODEL`。
`AGENT_TIMEOUT_SECONDS` 是整輪 Agent 期限；每次 CLI 呼叫另有 180 秒預設上限。
登入狀態檢查只查本機憑證，最多 30 秒。

## 呼叫與紀錄

Agents SDK 保留原有行情、帳戶、歷史、研究、支撐研究與反證工具。CLI 回傳下一個工具名稱與參數，
由 Python 執行允許的工具，再將實際結果交給下一輪 CLI；CLI 不直接接觸券商或下單。
所有既有候選、報價新鮮度、預算、人工／AI 持股與交易前複核仍由原程式驗證。

每個模型回合使用獨立、短期 CLI session，避免混用其他聊天內容。原有日誌檔名
`api_usage_requests.json` 等保留相容性；其中 `runtime=codex-cli`、`billing_source=chatgpt-plan`
與 token 數表示 CLI 回報的用量，並非 API 帳單，也無法換算剩餘方案百分比。
CLI 自帶指令與 schema 有額外輸入用量，多輪工具呼叫會增加 Codex 額度消耗。

CLI 在空白暫存目錄、read-only sandbox 執行，忽略使用者設定及 rules，停用 shell 與檔案修改工具，
不傳 API／券商環境變數。沿用使用者的正常 Codex 登入，不讀取或複製登入 token。
啟用原網路查證時改用 CLI 內建 web search，工具呼叫與來源依 CLI event 能提供的內容保存；
沒有來源資料時不會捏造。搜尋上限也會在回傳決策前核對。

逾時或取消會終止該次 CLI 與其 Windows 子程序，清除暫存輸入／輸出。非零退出、失敗事件、
缺少用量或不符 schema 都拒絕該次輸出。單元測試使用假的 CLI 輸出，不消耗模型額度。

官方依據：[非互動執行](https://learn.chatgpt.com/docs/non-interactive-mode)、
[ChatGPT 與 API Key 登入／計費](https://learn.chatgpt.com/docs/auth)。

## 2026-11-09 驗證紀錄

- `python -m unittest discover -s tests`：529 項全部通過，包含 11 項 CLI 傳輸與計費隔離測試。
- Codex CLI `0.166.0-alpha.6`、ChatGPT 登入、`gpt-6-sol`：實際取得符合 schema 的 JSON。
- 真實 SDK → CLI → Python 合成行情工具 → CLI：兩輪完成，結果為有效 WAIT，沒有券商連線或下單。
- CLI 內建 web search 實測成功；搜尋事件、來源解析與上限另有單元測試。
- `support_scout doctor`：`codex_chatgpt_login=true`、`ready=true`；PowerShell 入口語法檢查通過。

上述實測未啟動 Dashboard、自動掃描或正式交易；無法據此宣稱下一個交易日的實盤已驗證。
