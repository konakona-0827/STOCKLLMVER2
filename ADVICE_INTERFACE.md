# 建議資料介面

儀表板每完成一輪分析，就會產生一份穩定 JSON，供其他程式讀取：

- 最新資料：`data/analysis/latest_advice.json`
- 單輪封存：`data/analysis/runs/<run_id>/advice_interface.json`

檔案使用 UTF-8，寫入時先建立暫存檔再替換正式檔，避免讀取到寫入一半的 JSON。

## 主要欄位

- `interface_version`：介面版本，目前為 `1.0`。
- `run_id`、`run_status`、`run_error`、`llm_validation`：識別本輪和 LLM 驗證結果。`READY` 表示本輪 LLM 回覆通過程式驗證；`ERROR` 表示本輪有錯誤或沒有有效 LLM 回覆。
- `request_timing`：群益行情請求、LLM 請求與回覆、整輪完成時間。
- `market.quotes[]`：每檔行情的來源、買賣價、成交價、成交量、交易所時間、接收時間、資料年齡秒數、品質與警告。
- `recommendations[]`：每檔 AI 建議、`asset_type`（`STOCK`／`ETF`／`UNKNOWN`）、信心度、理由、參考／建議價格、建議股數、持倉管理股數及對應行情時間。
- `paper_simulation`：本輪模擬上限、可用現金、模擬持倉及本輪模擬成交；沒有完成模擬時為 `null`。
- `real_order_sent`：目前系統永遠不送出真實委託；此欄位會明確保留在輸出中。

行情新鮮度以券商 `exchange_time` 為準。即使 `received_at` 很新，若成交時間很舊，該行情仍會標為 `LAST_KNOWN`；下游程式可同時檢查 `quote_status`、`quote_age_seconds`、`warnings` 和 `run_status`。

## Python 讀取範例

```python
import json
from pathlib import Path

path = Path(r"data/analysis/latest_advice.json")
advice = json.loads(path.read_text(encoding="utf-8"))

print(advice["run_id"], advice["run_status"], advice["llm_validation"])
for item in advice["recommendations"]:
    print(item["symbol"], item["decision"], item["suggested_qty"], item["quote_status"])
```

`decision` 保留 `BUY`、`HOLD`、`SELL`、`WAIT` 等固定代碼，方便下游程式判斷；理由與警告使用中文。建議檔只供消費端讀取，消費端是否採取任何後續動作由使用者自行實作。
