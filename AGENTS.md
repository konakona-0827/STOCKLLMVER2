# 工作方式

- 預設使用繁體中文溝通與撰寫操作文件。
- 本分支依使用者 2026-10-28 指示研究日本股票；Yahoo Finance 為暫時來源，日線決策與未來券商執行隔離。保留原台股流程作相容性回歸用途。
- 專注尋找、證據與反證，保留獨立研究員／審查員；Head 用程式執行固定資格檢查。
- 所有改動限本專案；既有 STOCKLLM、STOCKLLM-agent、support-scout 由各自工作流程維護。
- 不載入帳戶或下單模組。OpenAI 僅使用明確設定的環境變數／本機設定；不得因沿用台股專案就自動複製或讀取其他專案憑證。值不得輸出、提交或抄入報告。
- 使用者已於 2026-10-28 明確授權沿用原本 ENV 的 OpenAI 設定；以忽略的 local_settings.json 指向 STOCKLLM/.env，僅載入 OpenAI key 與模型，不複製整份 ENV 或載入券商欄位。
- 日股預設由 JPX 動態母池經有界行情預掃、SEARCH agent 提候選，再交研究／反證 agent；固定名單僅能明示為手動模式。沒有 SEARCH 呼叫不得宣稱 agent 尋標。
- 使用者再次確認：標準決策流程只有「尋標、研究、反證」三個模型 agent，共同產出單一決策結論。Head與固定閘門都是程式，不另算agent，不新增第四個決策agent。
- 改規則或任務指令時保留版本、日期截止、固定支撐與重播測試。機器可驗證的事由 Python 處理。
- 有界 provider 失敗、資料不足或審查不完整時保留 WATCH；不得用模型敘述覆寫硬性檢查。
- 程式驗證：`python -m unittest discover -s tests -v`。離線示範：`python -m support_scout desk-demo`。
