# 正式持倉行情自動更新修復（2026-11-02）

## 實際原因

- 原自動 `Dashboard.launch_health()` 直接呼叫 `sync_broker_state()`，沒有手動重新整理才會提供的持股行情 enrich callback；因此券商資金、庫存對帳正常，持倉行情卻仍沿用先前快照。
- `current_position_quotes()` 只接受 120 秒內的行情。掃描每 30 分鐘一次，且 AI 分析可能超過兩分鐘，分析行情不足以持續提供持倉估值。
- 09:05 掃描收到 21 檔，但全部 `is_trial=true`，所以未呼叫 AI、未送單；09:35 收到 21 檔 `LIVE / is_trial=false`，完成分析。本次沒有 BUY/SELL 訊號，正式執行報告為 `NO_TRADE_ACTION`。

## 修正

- `AnalysisService.refresh_broker_state()` 共用手動與自動更新流程，在原 COM worker 查詢目前券商持股行情，並保存到原正式資料庫的 broker refresh snapshots。
- 盤中持有股票時，健康檢查完成後 60 秒再查詢；分析期間暫緩，分析完成後立即補查。非交易時段的自動檢查不新增行情訂閱。
- 查詢失敗會清除該輪行情並保存錯誤；部分缺報價、試撮或逾時狀態均明確標記。120 秒時效限制與送單前獨立風控保留。
- 行情頁顯示持股最新查詢，即使尚無分析結果也可顯示。試撮顯示「試撮（不可交易）」，舊資料顯示「最近行情」。系統資訊增加持股行情查詢時間。
- 無新增 LLM 呼叫；沿用原正式帳本、預算與持倉，無測試下單。

## 測試

- `python -m unittest discover -s tests -p test_position_quote_refresh.py -v`：6 項通過，涵蓋自動 worker、失敗清除、缺漏/試撮、空持倉、盤外與報價時效。
- `python -m unittest discover -s tests -p test_dashboard_trade_display.py -v`：3 項通過，包含新增的無分析結果行情顯示測試。
- 工作目錄完整回歸：368 項通過，42.441 秒（上述新增畫面測試另行通過）。
- 以 staged tree 匯出乾淨目錄，執行 `python -m unittest discover -s tests -v`：361 項通過，35.662 秒，包含本修正全部新增測試。
- 測試紀錄保存在本機 `data/backups/quote_refresh_fix_20261102/`，不提交帳戶原始資料。

## 正式程式驗證

- 原視窗正常關閉、原 supervisor 工作重新啟動，未建立第二個正式程式或新帳本。
- 09:42:18（台北）正式快照取得 1101、2449、2891 全部三檔 `LIVE / is_trial=false` 行情，錯誤清單為空；畫面已恢復持倉參考市值。
- 自動掃描保持啟用、下次 10:05（台北），無未結委託；原資金上限、持倉與正式紀錄保留。

## 操作限制

這是週期性持股報價更新，並非逐筆行情串流。券商沒有新成交、連線異常或分析尚在執行時，仍可能暫時無法提供 120 秒內報價；不以舊價格冒充可交易即時價格。行情成功不等於進場條件通過或委託成交。
