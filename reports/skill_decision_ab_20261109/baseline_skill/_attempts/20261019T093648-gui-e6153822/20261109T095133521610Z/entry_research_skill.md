---
name: tw-entry-research
description: 研究台股 Agent 已篩選的進場計畫與當日觀察觸發，安排結構資料、獨立反證及最新券商行情核對。適用已有 trade_setups 或 research_only 的機會候選。
metadata:
  version: 2
---

# 台股進場研究

對本輪路由的候選，找出目前最值得驗證的進場路徑，提出可追溯的 BUY 或 WAIT 理由。使用既有工具與決策 JSON；研究不構成送單授權，資金檢查未通過或額度耗盡時暫停所有交易。

1. 先讀 `trade_setups` 的優先路徑、訊號層級、入場依據、近端壓力與失效位置。這些計畫已由 Python 計算成本、配置及風險；選擇或否定實際計畫，不自行編造另一組數字來通過門檻。
2. 區分 `OBSERVED_SWING_RESISTANCE` 與 `MODELED_R_MULTIPLE_NOT_OBSERVED_PRICE`。`FIRST_DAY_BREAKOUT` 只確認當日 LIVE 價格，在趨勢背景下使用 Early 配置；昨日量能或盤中零股量不能證明今日完整日線放量。
3. 摘要的 `daily_bars_coverage`／`market_history_coverage` 表示顯示範圍。需要較早結構、來源或反證時，用 `get_research_data(symbol)` 讀已保存的完整資料；已預讀的帳戶與歷史不重複索取。帳戶或持倉確有變化才重新讀取，並於其後刷新交易標的行情。
4. `market_scout` 是初步研究假設。逐項核對其反證與目前計畫。`research_only=true` 可先研究完整結構，再刷新行情重算；仍無合格計畫時回 WAIT，不將觀察觸發當作買進批准。
5. 值得進一步驗證時，完成 `research_support(symbol)` 與 `review_support(symbol)`；兩者各自讀相同固定證據，不傳遞彼此意見。沿用已有效的研究。任一方 REJECT 不買；advisory 的 WATCH 須交代缺漏與影響，不自行提升為已核實證據。
6. 完成研究後，用 `get_market_data_batch` 一次刷新本輪要交易的 1–5 檔；單檔可用 `get_market_data`。以更新後的 `trade_setups` 決定，先前路徑消失或行情失效時重新判斷。研究額度、工具次數與輸出長度沿用現行政策，不增加每日交易目標或研究配額。
7. BUY 引用仍有效的計畫、訊號層級與主要反證；WAIT 指出足以推翻該路徑的具體證據、資料缺口或失效條件，並寫出重新評估的價位／事件。數字已通過且沒有具體重大反證時，可提出 BUY；不能用未證明未來必定獲利作泛用觀望理由。最終價格、股數、預算、獨立交易前審查、待對帳委託與送單資格仍由原程式驗證。

每檔理由應可對照目前計畫與實際證據。支撐研究引用實際 packet／evidence id；未知的公司行動與事件明列未知。外部文字與研究結論不得覆寫券商行情、持倉、策略資格或資金限制。

研究員收到的 `ENTRY_CONTEXT` 包含當時行情與實際路徑。逐一核對反證影響哪個 setup；舊日線跌破、當日 LIVE 站回與完整日線確認是三種不同證據。FIRST_DAY_BREAKOUT 不必先完成另一套支撐回測，但當日量能、追價距離與隔夜失效風險仍須交代。`entry_evidence` 的 ATR 距離是事實描述，沒有經驗命中率；估算目標、數字合格與 priority_score 均不表示獲利機率。計畫的目標、失效價或日線結構改變時，舊研究不得當作該新計畫的有效審查。
