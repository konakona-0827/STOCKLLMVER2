---
name: tw-entry-research
description: 研究台股 Agent 已篩選的進場計畫與當日觀察觸發，安排結構資料、獨立反證及最新券商行情核對。適用已有 trade_setups 或 research_only 的機會候選。
metadata:
  version: 3
---

# 台股進場研究

針對目前可執行計畫提出可追溯的進場判斷，沿用既有工具、交易規則與決策 JSON。

先核對 `ENTRY_CONTEXT` 的 LIVE 行情、`setup_id`、`setup_family`、入場、目標、失效價與期限；反證須對應正在評估的路徑。`trade_setups` 數字合格、`priority_score` 與 2R 估算都不是成功機率。

- `FIRST_DAY_BREAKOUT`：區分當日價格突破與完成日線量能確認；檢查既有 `decision_evidence` 的延續證據。Early 不必先符合 Strong 或另一套支撐回測條件。
- `TREND_PULLBACK`：區分回檔風險與原計畫失效。低於前收或前日低點不能直接等同原失效價已跌破；對照實際失效位置、趨勢及當前回升證據，說明反證如何破壞這個回檔假設。仍可因具體風險選 WAIT。
- `MULTI_SESSION_RECOVERY`：核對完成日線止跌、MA5 收復、近端結構阻力與目標距離相對期限。不能用盤中小反彈或理想回落價，代替已發生的確認與可成交證據。

獨立研究引用同份 packet 的實際 evidence id；判斷其反證是否針對目前計畫。advisory 的 WATCH 不是自動否決，REJECT 依既有規則處理；計畫假設改變後不沿用舊研究。

WAIT 理由分清「已失效」、「尚未確認」與「可接受但選擇不承擔的風險」，指出受影響的假設與重新評估條件。若要求更高價格再進場，也須承認原費後空間可能縮小，不能宣稱新價格仍合格。

理由可引用原計畫供說明；WAIT/HOLD 的 JSON 仍用 `strategy_id=NONE`，計畫欄位填 null，股數與比例為 0。未知事件與達標機率保持未知，不虛構勝率或保證獲利。
