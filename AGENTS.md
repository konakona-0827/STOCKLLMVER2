# 工作方式

- 預設使用繁體中文溝通與撰寫操作文件。
- 本專案第一階段僅研究台股。Advantest 為型態概念參考，不接日本行情。
- 專注尋找、證據與反證，保留獨立研究員／審查員；Head 用程式執行固定資格檢查。
- 所有改動限本專案；既有 STOCKLLM、STOCKLLM-agent、support-scout 由各自工作流程維護。
- 不載入帳戶或下單模組。使用者已明確授權只讀 STOCKLLM 的 OpenAI API key 與模型；以忽略的 local_settings.json 指定來源，值不得輸出、提交或抄入報告。其他憑證不在授權範圍。
- 改規則或任務指令時保留版本、日期截止、固定支撐與重播測試。機器可驗證的事由 Python 處理。
- 有界 provider 失敗、資料不足或審查不完整時保留 WATCH；不得用模型敘述覆寫硬性檢查。
- 程式驗證：`python -m unittest discover -s tests -v`。離線示範：`python -m support_scout desk-demo`。
