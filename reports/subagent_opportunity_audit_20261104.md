# 2026-11-04 子代理與買入機會核查

範圍：今日已完成的台灣時間 09:05、10:05 兩輪（日本時間 10:05、11:05）。本次核查未觸發新的模型請求或委託。

## 結論

今日子代理實際呼叫為 0 次，沒有進行子代理買入研究。這只能說明固定研究池沒有通過目前程式規則的買入計畫，不能推論整個台股市場沒有交易機會。

目前 Support Scout 的兩個角色是主代理指定標的的研究員、反方審查員；它們沒有自行擴展股票池或搜尋整個市場的入口。程式先篩選固定 20 檔，只將合格買進候選與現有 AI 持倉送給主代理。沒有合格計畫時，子代理可以完全不被呼叫。

## 今日實際流程

| 台灣時間 | 程式篩選 | 主模型 | 子代理 | 結果 |
| --- | --- | --- | --- | --- |
| 09:05 | 21 筆收到的報價均標記 is_trial=true；排名沒有可用買入候選，機會池僅剩持倉 1101 | 未呼叫 | 0 次 | 沒有委託 |
| 10:05 | 固定池 20 檔，合格買入計畫 0 檔 | 僅收到持倉 1101 | 0 次 | 1101 HOLD，沒有委託 |

10:05 的 `support_research` 明確記錄 `enabled=true`、`calls=[]`、`packets=[]`。啟用設定不等於實際執行。主模型該輪消耗 14,315 input tokens、807 output tokens、1 次請求；工具只呼叫 `get_portfolio`、`get_trade_history`。

## 10:05 的 20 檔為何沒有進入買入研究

| 前置排除原因 | 檔數 | 標的 |
| --- | --- | --- |
| NO_TREND_OR_RECOVERY_SETUP | 17 | 2881、2882、2345、2330、2603、2609、0050、3231、2317、2376、2454、2382、2303、2357、2356、2377、2308 |
| BUY_REENTRY_COOLDOWN | 2 | 2891、1101 |
| NO_COST_AND_RISK_QUALIFIED_ENTRY_IN_REACH | 1 | 3017 |

3017 奇鋐的當輪賣價為 2,925 元，開盤 2,910 元，日內區間 2,860–2,930 元，價格位於區間約 92.9% 處，符合程式盤中回升條件。完整日線 MA5=3,082、MA20=3,282，沒有符合多日趨勢，因此屬 Early；固定策略資本 10,000 元的 Early 單檔配置上限是 1,500 元，連一股現價與附近可達限價都無法容納。這是資金／策略條件限制，不代表沒有價格波動機會，也沒有證明放寬後能獲利。

17 檔的排除也不等於做過完整買入研究。例如 2881、2882 的現價各等於開盤價，即使已從當日低點回升，也因 `last > open` 的嚴格條件未通過盤中回升訊號。這是特定訊號定義，而非市場不存在機會的證據。

## 實作範圍與缺口

1. **股票池固定**：只掃描 20 檔，沒有全市場發現機制。擴大搜尋範圍應先由價格、成交額、可負擔性等程式條件產生候選，再集中模型預算。
2. **子代理介入較晚**：必須先通過預先寫死的可執行計畫篩選，才可能取得研究任務；目前沒有「接近條件但尚未可買」的觀察研究流程。
3. **策略覆蓋不完整**：資料層列出 `short_term_reversal`、`breakout` 等可評估策略，但 `opportunity_engine.refresh()` 目前只產生 `multi_day_trend` 或 `intraday_recovery` 計畫。資料足夠不等於已有對應的買進計畫產生器。
4. **觀察條件未結構化追蹤**：不符合條件的股票只留下排除理由，沒有獨立的近條件觀察清單、具體觸發價格與到期時間。

改善應針對候選發現、策略覆蓋與觸發追蹤；不應用增加付費研究次數或強制交易來證明積極。以上是架構診斷，本次沒有調整正式交易門檻或擴大自動買入範圍。

## 證據

- `data/analysis/runs/20261104T090500-gui-5e631bba/run.json`
- `data/analysis/runs/20261104T090500-gui-5e631bba/raw_quotes.json`
- `data/analysis/runs/20261104T090500-gui-5e631bba/opportunity_search.json`
- `data/analysis/runs/20261104T100500-gui-28e0efb9/run.json`
- `data/analysis/runs/20261104T100500-gui-28e0efb9/opportunity_search.json`
- `data/analysis/runs/20261104T100500-gui-28e0efb9/opportunity_pool_before_selection.json`
- `data/analysis/runs/20261104T100500-gui-28e0efb9/agent_response.json`
- `data/analysis/runs/20261104T100500-gui-28e0efb9/selection_decision.json`
- `support_scout/trading.py` 的 `SupportCoordinator.instructions()` 與 `prepare()`
- `opportunity_engine.py` 的 `refresh()` 與 `select()`
