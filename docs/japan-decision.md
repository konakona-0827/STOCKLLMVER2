# 日股決策層

`decision.attach_decisions(report, bundle, *, policy=None)` 在原報告每檔加入 `trade_decision`，並回傳同一報告。呼叫端先完成數值掃描及研究審查，再產生結論；每日觀察在跨日帳本 callback 掛上 `report['ledger']` 後重算。`render_decisions_markdown(report)` 提供報表段落。

| 結論 | 意義 |
|---|---|
| `CONSIDER` | 支撐、資料、雙方審查與情境淨風險報酬均通過，可進入人工交易評估 |
| `WAIT` | 型態、資料或審查尚有缺口，繼續等待 |
| `NO_TRADE` | 支撐失效、否決、追價、資料被拒或情境風險報酬不足，本輪不交易 |

這些結論沒有訂單權限，`execution_enabled` 固定為 false。`CONSIDER` 也不能沿用至下一交易時段，必須用新報價、公司公告與資料重新驗證。帳戶、持倉與可信損益均尚未串接，因此沒有實作 5% 帳戶停工；單支股票跌幅不能代替帳戶損益。

## 固定決策門檻

非合成、非 observation-only、資料完整；支撐已確認且接近支撐、無技術與背景阻擋；同一完整 packet 的研究員及反證員各自回覆有效 `RESEARCH`。兩方評語重新驗證，packet 也重驗完整性。跨日帳本發現歷史價修訂、已失效支撐、倒退資料或錯誤時再次降級，不能讓每日重算的新支撐覆寫舊帳本的反證。

Yahoo 暫用資料保留 observation-only、公司行動／事件／交易狀態缺口時，即使雙 agent 都贊成也不會出現 `CONSIDER`。報告仍輸出是否等待或不交易、原因及下一步條件，不用一段模型文字掩蓋資料缺口。

## 可追溯的價位與情境風險報酬

參考入場是最後完整日線收盤，不是可成交即時報價。固定失效價取原支撐下緣，代表「日線收盤跌破則型態失效」，不是已掛的停損單，也不保證出場價。

目標取支撐 anchor 之前 20 根、截止前已可見日線的最高價。報告列出最高價當日、資料可得時間、窗口、來源與 packet ID；不以入場價加兩倍風險捏造目標。歷史不夠或前高不高於參考入場價時不能通過。

`DecisionPolicy` 預設要求情境淨 RR 至少 2，來回成本 20 bps，另加建立支撐時 ATR 的 0.25 倍作跳空緩衝。這些是可版本化的研究假設，尚未代表券商費率或損失上限。

```
nominal_risk = entry_reference - frozen_support_lower
cost = entry_reference × roundtrip_cost_bps / 10,000
gap = frozen_support_atr × gap_buffer_atr
gross_RR = (historical_resistance - entry_reference) / nominal_risk
net_RR = (historical_resistance - entry_reference - cost) / (nominal_risk + gap + cost)
```

高分、確認支撐與 RR 均未經樣本外績效驗證；RR 是情境價位比例，不是勝率、期望報酬或保證最大虧損。未來應以無未來資料的逐日快照，檢驗跳空、公司行動、事件日、實際交易成本與未成交情境，再調整門檻。

## 驗證

`python -m unittest discover -s tests -p test_japan_decision.py -v` 檢查雙方同意仍不能提升 Yahoo 觀察資料、反證否決、固定支撐失效、追價、成本壓縮、跨日修訂／倒退／錯誤、packet 竄改與未來價格尖峰不改變目標。測試中宣告為完整資料的 fixture 仍是人工造資料，只用於驗證門檻，沒有真實股票推薦。
