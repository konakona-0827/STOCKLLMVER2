# 日股行情資料轉接

`support_scout.japan.build_snapshot(symbols, cache_dir, *, months=8, timeout=15, client=None, progress=None)` 保留原觀察流程的 bundle 介面。來源先採 Yahoo Finance chart JSON 公開讀取端點，每檔每輪一次請求，不自動重試、不依賴券商、不沿用舊快取。Yahoo 不是 JPX 官方行情 API，之後可以替換 builder；程式不含登入或下單功能。

代碼正規化為 `6857.T`，支援英數代碼如 `130A.T`。回應必須符合指定代碼、東京交易所、`Asia/Tokyo`、JPY、EQUITY、1d；缺欄、錯代碼、零量、漏日或過期尾端均阻擋該標的。失敗標的仍留在報告，不會因其他成功資料而消失。EQUITY 不代表已獨立核實普通股身分，因此保留分類阻擋原因。

日線成交值是 `close × volume` 的日圓估算，使用 `turnover_jpy` 與 `turnover_estimated=True`，不冒充實際成交金額，也不寫進台幣欄位。OHLC 直接取 quote 欄位，不混入 adjusted close。Yahoo 的歷史調整口徑未核實，`price_basis=raw_unverified` 只是研究系統的未知口徑標記；不代表 Yahoo 保證原始價。窗口內有已知拆股或資本利得事件時整檔待覆核；事件清單空白也不解除公司行動未知。

獨立日曆採版本 `jpx-cash-2025-2027-verified-20261028`，按 JPX 2026/2027 休市表及內閣府 2025 祝日 CSV 核對，加上 JPX 年初／年末休市。超出 2025–2027 年份會失敗，必須更新版本與日期；不自行假設一般平日就是交易日。這份版本化表沒有臨時停市動態來源，預期開市卻沒有完整行情時會阻擋，不能改稱假日。

東京收盤為 15:30；為延遲行情保留至 16:00 JST 的發布緩衝，16:00 前排除當日日 K。應有最新交易日從獨立日曆推導，`actual_latest_session` 只取成功行情尾端，全部失敗時為 null。抓取跨過16:00或跨日會要求重跑。

每次原始 JSON 以唯一檔名存入 cache_dir，保存 URL、fetched_at 與原始 bytes 的 SHA256。快取僅作稽核檔，不作斷線備援。日線 `available_at` 與 `adjustment_as_of` 使用此次組裝時間，保留誠實的當前可得性，不宣稱今日下載的歷史行情是歷史當時可得快照。公司公告、財報事件、市場壓力、交易狀態與價格調整均尚未完整核實，因此這個 provider 永遠產生 observation_only 與明確阻擋原因。

來源：[JPX 休市表](https://www.jpx.co.jp/english/corporate/about-jpx/calendar/index.html)、[JPX 交易時段](https://www.jpx.co.jp/english/equities/trading/domestic/01.html)、[JPX 普通股單元](https://www.jpx.co.jp/english/equities/trading/domestic/03.html)、[內閣府祝日](https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html)。

離線測試：`python -m unittest discover -s tests -p test_japan.py -v`。
