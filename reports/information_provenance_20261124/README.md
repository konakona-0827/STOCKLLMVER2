# 缺資訊的來源追查與資料交付修正

本次基於 caf6eb6。使用者要求區分「市場沒有資料」和「系統沒有取得或交給模型」，並修正可確認的缺口。結論是兩個系統端交付缺失已確認，部分供應端缺值仍無法歸因為市場不存在資訊。原始實機依據截至台灣 11/24 10:45，見 [先前盤中紀錄](../intraday_quote_gap_20261124/README.md)；本輪沒有新的交易電腦實機快照，不回填歷史缺價原因。

| 資訊 | 原本實況與歸因 | 本次處理 |
| --- | --- | --- |
| 盤中時間序列 | LegacyRuntime 已保存最多 60 筆快照，研究封包只交付當前一筆；零股成交歷史介面尚未串接。屬系統交付／取得缺口。 | 快照持久保存、同會話訂閱當日成交回補，研究、複核與持股管理都可取得來源與歷史。 |
| 較長日線 | Python 計算使用最多 60 根連續完成日線，研究模型卻只有近八根原始日線與摘要。屬系統資訊裁切。 | E_DAILY_HISTORY 交付本次實際使用的完整區段，以表格壓縮，不另外製造或補齊缺日。 |
| 買一、賣一、成交價、日低 | 歷史快照顯示缺賣一及賣量零，但 Git 證據缺少逐筆 SDK 原始欄位，不能斷言市場沒有掛單。 | 保存原始欄位值、縮放位數、取得／驗證／轉換階段與責任來源。正值被系統遺失會單列程式缺失。 |
| 公告與即將發生的公司行動 | 已串接四個官方現況來源；本輪讀取分別有 110、23、28、66 列，目前欄位可對應。 | 新增來源格式驗證；欄位變動或解析失敗不會再表現成「零公告」。 |
| 完整新聞歷史及歷史還原價 | 現有來源範圍有限，尚未完整串接或認證，屬系統能力限制。 | 明列 SYSTEM_NOT_INTEGRATED／來源覆蓋限制，維持原風控與研究支援政策，不宣稱市場不存在或已完成認證。 |

## 成交資料如何進入模型

依 [群益官方零股成交明細文件](https://www.capital.com.tw/apiforum/docs/atlObjFunction/domesticQuote/realTimeQuotesForOddLot/getQuoteTicksWithMarketNo)，指定市場的成交明細訂閱可接收當日歷史及後續成交事件。原系統未呼叫此介面，所以先前「沒有分鐘資料」不能歸咎於市場。

已使用原 COM owner 的同一 SKQuoteLib，透過 SKQuoteLib_RequestTicksWithMarketNo 讀取市場 5／6。依隨附 V2.13.59 文件 4-4-34，頁碼從 0 開始；每個商品一頁，分批最多 10 檔。官方亦列明 [Tick／五檔訂閱限制](https://www.capital.com.tw/apiforum/docs/apiCOM/quoteObj_SKQuoteLib)。本次只新增證券行情讀取，沒有新登入、送單、撤單或期貨路徑。

事件回呼只入佇列，SDK 查詢與轉換在原 STA 執行緒處理。排除試撮、錯市場／股票、跨日、未來時間及非法量價。歷史與即時事件依原鍵值去重，保留已收到的成交，彙整為分鐘 OHLC、股數與筆數；形成中的分鐘不交作已完成分鐘，缺少的分鐘不補值。行情外層期限按十檔分組計算，仍有有限等待、超時隔離及原程序恢復規則。

歷史回補沒有本次可據以認證完整性的結束證據；讀取採有限回應窗口，標示 RECEIVED_TICKS_ONLY_NOT_CERTIFIED_COMPLETE。API 回傳成功、已收到數筆或看見若干分鐘都不等於收齊全部交易。快照也不冒充分鐘 K，普通交易價格不冒充零股證據。此限制在研究、複核與持股管理提示詞中一致說明。

## 缺資料如何歸因

- PROVIDER_EMPTY_FIELD：SDK 原始欄位為零／空的有效值；只能確認券商沒有提供有效值，market_absence_confirmed 仍為 false。
- SYSTEM_TRANSFORM_LOSS：原始價格存在，但系統交付缺值；單列程式轉換缺失，不能偽裝為正常市場等待。
- SOURCE_VALIDATION_REJECTED：原始數值存在，但時間、身分或其他檢查沒有通過；不當成轉換遺失或可交易價格。
- SOURCE_NO_RESPONSE／來源請求錯誤：已請求但未得到可驗證回覆，保留來源錯誤，不能宣稱市場沒有資訊。
- SYSTEM_NOT_REQUESTED／系統介面或解碼錯誤：明列系統取得能力缺口；無原始欄位證據時為 SOURCE_TRACE_UNAVAILABLE，維持未知。

E_DATA_LIMITS.information_availability 保存提供給模型的資料種類、數量與來源。介面研究卡增加「資料來源與缺口」；模型若要求分鐘證據，research_information_gaps 與 summary.information_gap_origins 分開記錄。即使價格不變，新出現的可用時間證據也會重新排研究；重複成交或純時間戳不會逐筆觸發模型。待確認的新資料不會被自動改成 RESEARCH 通過。

實機逐檔診斷位於原狀態目錄 broker/latest_buy_quotes.json 的 information_trace、intraday_history。研究封包另保存 E_DAILY_HISTORY、E_INTRADAY.history 及 information_availability。快照與成交持久檔為 broker/intraday_evidence.json，重啟仍可使用經當日／時間／格式核對的紀錄。持續寫檔失敗會標示儲存缺口，記憶體資料仍可追查。

## 驗證及限制

最終完整離線回歸 **933 項通過，失敗 0、錯誤 0、略過 0**；新增資料來源針對性測試 **22 項通過**。原 OpportunityFlow.cmd -Mode Check 回傳 0、ready=true，沒有登入券商或實盤送單；隔離 Windows 排程測試完成後沒有殘留測試排程。

新增針對性測試涵蓋來源零值與系統遺失區分、錯時資料、原 SDK 呼叫與事件處理、0–9 頁及十檔限制、拒絕回應不丟失行情、成交去重與分鐘缺口、重啟資料、寫檔失敗、雙角色完整輸入、資料補齊後自動重審、來源格式變動，以及公開參考價仍不能授權送單。完整回歸與原啟動器 Check 結果見 verification.json／full_regression.txt。

本輪沒有連線交易電腦或用真實下單作測試，新增的成交回補路徑以原 SDK 介面的隔離替身驗證；交易電腦拉取、由 OpportunityFlow.cmd 啟動後，仍須由新實機診斷確認實際回傳與覆蓋。歷史原生崩潰根因與完整新聞／還原價認證不在本次宣稱已解決之列。不能保證所有未交易都合理，也不能保證此版本已不存在任何系統缺失。
