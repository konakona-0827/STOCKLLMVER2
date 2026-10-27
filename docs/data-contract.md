# 台股日線與資料合約

## 快照 JSON

執行 `python -m support_scout demo`，可在 `output/latest/input.json` 取得完整可執行格式；其中所有資料皆為合成。正式資料應使用相同欄位並將 `synthetic` 設為 `false`。

```json
{
  "schema_version": 1,
  "as_of": "2026-10-27T16:00:00+08:00",
  "universe_version": "tw-common-stock-v1",
  "synthetic": false,
  "calendar": {
    "source": "經核對的 TWSE/TPEX 交易日曆快照識別",
    "available_at": "2026-10-27T15:00:00+08:00",
    "coverage_through": "2026-10-27",
    "sessions": ["2026-10-27"]
  },
  "market_context": {
    "source": "市場背景資料來源識別或 URL",
    "available_at": "2026-10-27T15:00:00+08:00",
    "stress": "ELEVATED"
  },
  "securities": []
}
```

上例只說明外層欄位；真正掃描必須有完整交易日曆、非空 securities 及至少 60 根日線。交易日曆涵蓋輸入第一根 K 到截止時間；颱風、臨時休市及補假都必須來自實際日曆，不用週一至週五猜測。

## 每支標的

| 欄位 | 約定 |
|---|---|
| `symbol`, `name` | 台股代號與名稱，不用 Yahoo 的 `.TW` 或 `.TWO` 後綴 |
| `exchange`, `currency`, `instrument_type` | `TWSE` 或 `TPEX`、`TWD`、`COMMON_STOCK` |
| `interval`, `volume_unit` | `1d`、`shares`，不能把張數當股數 |
| `source` | 真正的資料來源／快照識別，保留原始檔供查證 |
| `price_basis` | `adjusted_ohlc` 或 `raw_no_actions_in_window` |
| `actions_reviewed` | 必須 true；只有真的核對研究窗口內公司行動後才可宣告 |
| `adjustment_as_of` | 調整版本當時已可得時間，必須不晚於研究截止 |
| `volume_comparable` | true 代表跨期成交量可比較；拆併股量能不能直接混比 |
| `tradability_reviewed` | true 代表核對一般交易／停牌／鎖漲跌停等來源欄位 |
| `point_in_time` | true 代表有來源證明當時可見；false／缺少時只能觀察 |
| `bars` | 依 session 升冪且唯一的完整日線 |
| `dossier` | 公司論點與事件查核，見下方 |

價格一律同一基準；還原 high/low/open/close 必須成套。未還原價格只有在完整窗口確認**沒有**影響跨日可比性的公司行動，才可用 `raw_no_actions_in_window`。`turnover_twd` 必須是來源成交金額，不能用還原價格乘原始股數估算。

每根 K 欄位：

```json
{
  "session": "2026-10-27",
  "closed_at": "2026-10-27T13:33:00+08:00",
  "available_at": "2026-10-27T15:00:00+08:00",
  "complete": true,
  "open": 100, "high": 103, "low": 99, "close": 102,
  "volume": 1000000, "turnover_twd": 101000000,
  "halted": false, "limit_locked": false
}
```

`closed_at` 是來源確認交易完成時間，範例用 13:33 只是顯示可含延後收盤，adapter 不得只因時鐘過 13:30 就偽造 complete。`available_at` 是該版本資料真正可得時間。必須依 session 非遞減；延遲補登／更正要建立新的快照，不能回填歷史可得時間。

全量檔可以包含截止以後 K，但不會進入特徵或快照雜湊。過去應有而尚不可得的 K 會導致 coverage 失敗。停牌／零量資料不填前值；第一版拒絕該段，adapter 應另建立足夠長的新連續交易段。

## 公司與事件資料

```json
{
  "source": "人工或來源程序完成查核的紀錄識別",
  "available_at": "2026-10-27T15:00:00+08:00",
  "thesis_status": "INTACT",
  "events_reviewed_from": "2026-10-27T13:30:00+08:00",
  "events_reviewed_through": "2026-10-31T23:59:00+08:00",
  "events": [
    {
      "kind": "earnings",
      "event_at": "2026-10-29T16:00:00+08:00",
      "announced_at": "2026-10-20T10:00:00+08:00",
      "source": "公司公告 URL 或保存的來源識別"
    }
  ]
}
```

`thesis_status` 為 `INTACT / BROKEN / UNKNOWN`。沒有消息不代表 INTACT。市場與公司查核超過七個日曆日即過時；`events_reviewed_from` 必須不晚於最新完整 K 收盤，`events_reviewed_through` 至少覆蓋截止後三個日曆日。未來三日有已知重大事件先觀察。事件發生與公告的較晚時間，若落在最新完整 K 收盤之後至研究截止之間（含邊界），也維持觀察；即使休市多日，都不能假設最後那根 K 已反映事件。事件表必須含這整段歷史，不能只查未來事件。

## 接現有專案

檢視到的 `STOCKLLM/daily_history.py` 是 TWSE 未調整日線來源，最多保留 22 根，`stockprototype/quotes.py` 則是 `1m_snapshot`；兩者都不能直接宣告已滿足新合約。

下一階段 adapter 必須：

1. 只讀取行情匯出資料，不匯入帳戶／下單模組。擴充取得至少 60 根（建議 120–260 根）完整上市／上櫃日線。
2. 對 `date → session`、OHLC、股數、**實際成交金額**映射；不能從既有函式輸出的有限欄位猜出未保存數值。
3. 分別處理 TWSE 與 TPEX，不讓上櫃股票誤走上市資料接口。
4. 接公司行動及還原價格、停牌／鎖漲跌停與實際日曆。無法查核就保留未知及拒絕原因。
5. 今天下載的歷史檔，其 available_at 為今天真正取到的時間；不能改為歷史收盤日。技術掃描可觀察，但未有 PIT 證明之前不能稱為歷史策略驗證。

第一階段刻意停在這份合約與可運行的掃描入口，未自動讀取或改寫原專案資料。

官方參考：[TWSE 交易制度](https://www.twse.com.tw/zh/products/system/trading.html)、[TWSE 除權除息計算結果](https://www.twse.com.tw/zh/announcement/ex-right/twt49u.html)、[TWSE OpenAPI](https://openapi.twse.com.tw/)、[TPEX 常見問題](https://www.tpex.org.tw/zh-tw/about/company/faq.html)。
