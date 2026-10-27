# Support Scout 正式交易整合與驗證

日期：2026-10-27。本文時間以使用者端 UTC+9 表示；程式原始紀錄採台北 UTC+8。

## 分支及來源

- 目前分支 `codex/support-scout-architecture`，原始版本 `a3e5a50`。
- 正式交易來源 `fix/ai-sellable-ownership-display@46c020c`，包含 `develop@363fe8f` 與後續 Agent／券商資金／交易日修正。
- 已重新 fetch origin，確認上述來源仍為遠端最新版本。
- 以合併保留雙方歷史，選入正式程式、設定、策略、SDK 及測試；不匯入 Git 裡舊的交易資料檔。
- 原有嚴格研究核心 `contracts.py`、`support.py`、`pipeline.py`、`agents.py` 保留。

## 已完成

1. 主 Agent 真正使用 `Agent.as_tool` 呼叫研究員與獨立反證員。預設 SOL；兩方讀同一固定快照，身分與時間由程式產生，不能共享意見或下單。
2. BUY 需要兩方有效審查，任一 REJECT 會阻擋。預設 advisory 允許 WATCH 輔助原有策略；SELL 繼續使用原本退出風控。
3. 正式券商執行、委託／成交對照、AI 與人工持股歸屬、預算複查、零股交易日限制及交易前審查均接上既有底座。
4. `execution/`、`position_analyzer.py`、`pretrade_policy.py`、`runtime_paths.py` 與 `config/execution_live.json` 對來源版本沒有差異。新增研究閘門位於決策整合層。
5. 支撐研究擴充至 100 根日線，使用真實成交金額。用 TWSE FMTQIK 實際市場交易日核對，處理年度行事曆未列的臨時休市；個股真正漏日仍拒絕。收盤後不重用缺少當日資料的早盤快取。
6. `Dashboard.cmd`／`RunSupport.ps1` 啟動 Agent 模式及分支策略；CLI 保留 demo／scan，新增 doctor／live。
7. JSON 畫面快取遺失時，直接讀正式資料庫還原歷史分析與策略理由，標示為歷史顯示；不生成可執行建議或冒充最新券商對帳。

## 帳戶狀態

沿用同一正式 `.env`、分析 SQLite 與執行 SQLite。整合前以 SQLite backup API 備份至父目錄 `.state-backups/support-integration-20261027T215529/`。憑證、資料庫與執行紀錄均不提交 Git。

整合後讀到 18 筆正式交易嘗試與 130 輪分析；兩個資料庫 `PRAGMA integrity_check` 均為 `ok`。沒有建立獨立的 Agent 資金或持倉帳戶。

22:09 的第一次唯讀登入回傳 157。已透過 SDK 本身與隨附登入代碼文件確認其意義為「未能取得交易帳號」，當時正確記為 ERROR／資金未驗證；未繞過風控。22:24:48 再做一次獨立唯讀複查成功：

| 檢查 | 結果 |
|---|---|
| 券商健康 | OK，errors=[] |
| 券商持股驗證 | true，全部 MATCH |
| 預算狀態 | READY |
| 券商餘額 | NT$25,634 |
| AI 額度 | NT$10,000 |
| AI 已占用資金 | NT$4,566.50 |
| AI 可買預算 | NT$5,433.50 |

此為當次實際查詢，不推定第一次錯誤的外部原因。

## 驗證證據

| 項目 | 結果／範圍 |
|---|---|
| `python -m unittest discover -s tests -v` | 295 項通過 |
| `python -m unittest test_advice_interface -v` | 3 項通過 |
| `python -m pip check` | 無依賴衝突 |
| `python -m support_scout doctor` | ready=true；SDK 0.23.1；憑證設定及既有帳本可讀 |
| 原有 demo | 成功產出報告，原始 42 項研究測試保留 |
| 真實 OpenAI API | `gpt-6-sol` 主 Agent 與兩個子 Agent 均成功；兩方回 WATCH，最終決策 VALID |
| SDK 行為回歸 | 真正的 SDK manager／as_tool 流程，模型以腳本替身；驗證獨立輸入、不可偽造 evidence、否決、逾時、呼叫預算、帳戶改變後失效 |
| 正式執行整合回歸 | 模擬券商，通過 BUY→HOLD→SELL、相同帳本更新、送單前資金降低阻擋、重複單防護、LLM／Agent 切換 |
| 真實市場資料 | 1101，100 根日線，最新 2026-10-27；8 個月 FMTQIK 來源均成功、日曆 verified=true |
| Dashboard | Tk 啟動、本地歷史還原、Agent 模式與子代理結論顯示通過；無 callback error |
| 券商唯讀對帳 | 最終 OK，庫存 verified，budget READY |

測試沒有送出實際委託。真實模型煙霧驗證使用明確標示的合成資料，未連券商；GUI 煙霧驗證沒有連模型或券商。正式買賣呼叫使用模擬券商驗證，不宣稱本輪已成交。

本機證據保留於父目錄 `support-final-regression.txt`、`support-real-sdk-verification.txt`、`support-real-market-verification.txt`、`support-gui-verification.txt`、`support-broker-final-verification.txt`，及忽略的 `data/analysis/agent/support-*-smoke-*/verification.json`。

## 保留的研究限制

即時 adapter 是未調整原始價格觀察，不具備完整公司行動、公司事件或交易限制認證；嚴格 confirmed_support 不會假裝通過這些條件。歷史回測的當時可得性、上櫃資料接入及策略績效驗證仍屬後續研究工作。這不妨礙 advisory 模式使用原有可用策略及既有正式交易風控。

## 使用

在程式資料夾雙擊 `Dashboard.cmd`，或執行 `.\RunSupport.ps1`。預設 Agent 模式，啟動不自動掃描；分析按鈕會送交正式風控並可能下單。唯讀整輪測試使用 `.\RunSupport.ps1 -ReadOnly`。
