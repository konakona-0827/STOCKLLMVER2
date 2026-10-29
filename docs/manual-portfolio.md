# 手動成交回報與持倉研究

持倉只來自使用者明確確認的實際成交，不連券商，也不因研究建議、自動勾選或推測而新增／減少數量。沒有確認的買單和賣單都不改持倉；即使未確認賣單數量大於持倉也不產生做空。確認賣出超過已確認持倉則拒絕保存。

## 介面

`record_fill(path, entry, now=None)` 保存本地宣告；`read_snapshot(path, as_of)` 產生截止當時持倉。基本entry為：

```json
{"id":"manual-20261029-001","symbol":"6857.T","side":"BUY","quantity":100,"price":30000,"fees":0,"filled_at":"2026-10-29T10:00:00+09:00","confirmed":true}
```

`id`是冪等鍵，相同id與內容重送不重複記帳，不同內容則拒絕。紀錄為append-only；已存的未確認草稿不能同id改成另一份成交，確認操作應送出新的成交id。前端也可只在確認勾選時才呼叫寫入。填入的數量、價格和時間不能是負值、bool、NaN或未來值。

每筆另存`recorded_at`及遞增`sequence`。只有 `confirmed=true`、成交時間和紀錄時間都不晚於截點的資料才可見，事後補填不能出現在較早的回放。部分賣出按平均成本減少帳面成本，手續費納入買入成本；完全出清後再開倉會重設開倉日。只有使用者確認的新增買入才增加數量，程式不自行攤平或加碼。

快照含 `positions`、`confirmed_fills`、可見明細hash及整份digest。`verify_snapshot(value, as_of=None)` 會重算確認明細的持倉，不只驗hash；傳入as_of時必須與快照同一時點。整合時應由pipeline驗證快照、將單檔持倉及快照digest綁到agent packet，在帳本summary確定後再呼叫 `attach_manual_actions(report)`。

這是使用者宣告與一致性驗證，不是券商成交證明。hash無法阻止有意改造整份明細並重新計算hash的人；不能宣稱已做券商對帳。

## 原風險基準

成本只是持倉資訊，不是續抱理由。entry可選填`risk_reference`，明確記下持倉適用的風險定義：

```json
{"channel":"MANUAL","lower":29400,"price_basis":"raw_unverified","defined_at":"2026-10-29T10:00:00+09:00","anchor_at":null}
```

`channel`可為`MANUAL`、`ATR`或`H-001`。後兩者另須填`anchor_at`，並與可見情境的固定支撐下緣一致；H-001百分比支撐和ATR支撐不能混用。後續買入帶入不同基準會標記`CONFLICT_REVIEW`，不能偷偷下移原風險價位。沒有可靠基準時保持`REVIEW`，模型持倉評語仍會保存在 `agent_position_views`。

不會因偵測器曾有某個歷史ATR episode失效就一律叫賣。只有與這份持倉綁定的已知風險基準，配合成交後可見的新日線，才可依收盤跌破條件提出`EXIT_REVIEW`。這是人工退出評估，不是停損單，不保證可在該價成交。

## 持倉動作

- `HOLD`：已確認持倉、原基準、有效雙方評語及資料檢查均無阻擋，兩方才可提出續抱評估。
- `REDUCE_REVIEW`／`EXIT_REVIEW`：供人工減碼／退出評估；有分歧會另列`POSITION_AGENT_DISAGREEMENT`，不自動成交。
- `REVIEW`：沒有基準、原行情早於最近成交、資料過期、帳本修訂／倒退、評語缺失或資訊不足。

本輪未掃描到的既有持倉仍列在`manual_actions`，標記 `PORTFOLIO_SYMBOL_NOT_OBSERVED`，不會消失或被當成出清。固定觀察池可保持20檔，另外補抓已確認持倉只用於持有／退出研究，不等於擴大尋標池。

驗證：`python -m unittest discover -s tests -p test_portfolio.py -v`。所有測試成交、價格與模型評語均為人造，不觸發券商、網路或郵件。
