2026 年 11 月 18 日交易電腦更新與部署驗證

已拉取遠端 `9807e59`，並部署至原有 `codex/new-system-tw-opportunity-flow` 分支。這版補上 Windows 重開機後的恢復入口、程序守護、人工停止狀態保存，以及目前程序、新對帳與監看迴圈的實機報告。

實機首次啟動時，官方日線下載出現 `CERTIFICATE_VERIFY_FAILED / Missing Subject Key Identifier`。新的守護程序直接執行標準 FLOW 入口，原先本機啟動包裝器中的 Windows 憑證設定未被載入。已在 `9c1ae54` 將 Windows 信任憑證庫設定加入標準 `collect`、`quotes`、`live`、`watch` 入口，並宣告 Windows 的 `truststore` 相依套件。

修正後，正式 CLI 入口的官方公開行情 HTTPS 查詢成功。原程序經正常停止後重新啟動，11/18 官方上市櫃日線於台灣時間 18:53:34 更新成功，共 5 次 HTTP 請求及 131 筆快取命中。先前兩筆憑證錯誤保存在歷史紀錄中。

日本時間 19:54 的實機報告為 `WAITING_RESEARCH_WINDOW`。目前程序心跳、執行版本、新券商對帳、研究監看迴圈與自動執行流程皆已確認，盤外等候下一個研究時段。新買額度狀態為 `READY`，可用額 NT$9,534，最低保留額 NT$5,000，AI 上限 NT$10,000，單日買進上限 NT$30,000。今日正式自動送單為 0 筆。

Windows 排程註冊回覆 `ACCESS_DENIED`，程式已實際建立並讀回驗證目前使用者的登入啟動捷徑，生效觸發方式為 `OWNER_LOGON`。常駐守護程序已啟動。完整開機與每分鐘排程的註冊仍受本機權限限制，重新開機後須登入原 Windows 使用者工作階段。實機重新開機測試及下一個交易時段的即時研究、送單驗證仍待觀測。

修正後在隔離環境執行 846 項離線回歸測試，全部通過。實機對帳與日線更新也已通過。此次部署未產生新的正式自動委託。

- [verification.json](verification.json) 保存原遠端版本、實際執行版本、啟動方式、新對帳、HTTPS 驗證及日線更新事件。
- [regression.txt](regression.txt) 保存修正後完整的離線回歸結果。
- 執行中的即時報告持續寫入 `reports/opportunity_flow_runtime/latest.txt` 與 `latest.json`。本資料夾保存的是本次部署完成時的固定快照。
