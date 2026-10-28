# 從官方母池輪巡尋找日股標的

`load_universe(cache_dir, *, timeout=15, client=None, now=None)` 每次下載 JPX「東証上場銘柄一覧」XLSX，使用 Python 標準庫 urllib、zipfile、ElementTree，沒有新增第三方執行期套件。只納入 Prime、Standard、Growth 的內國股票四字元證券代碼；ETF、ETN、REIT、基金、外國股、出資證券、PRO Market，以及五字元優先／種類股排除。保留排除數量與原因、原始市場分類、33／17業種、規模分類及來源列日付。這是官方月末名單，仍需檢查當前上市／停牌與公司事件。

母池包含完整有序 `items` 及相同順序的 `symbols`（標準化 `.T`），先排 TOPIX Core30、Large70、Mid400，再排其餘股票；同層按證券代碼穩定排序。`size_category` 只作掃描優先順序，不能保證流動性；真正流動性仍由 Yahoo 日線數值閘門決定。`ordered_items_digest` 與 `universe_id` 綁定原始 XLSX hash、來源日期及排序版本。

`select_cohort(universe, *, limit=100, cursor=0)` 回傳 `(symbols, next_cursor)`，每批最多100檔，以配合行情 adapter。最後一批只回傳剩餘標的，再將 cursor 歸零，不補入下一輪標的。例如245檔依序分成100、100、45檔，全部輪巡後才重回第一檔。主流程持久化 universe_id、cursor、last_symbol、名單基準日及revision；換版時從上次標的之後接續，若該標的消失則接續原索引位置，不每月重置前100檔。晚完成的舊月份不能覆寫新月份。revision CAS防止整輪繞回後舊程序誤推進。超界 cursor 明確失敗。

名單的 `as_of` 是 XLSX「日付」，`fetched_at`／`available_at` 是這次實際取得時間，兩者分開。依 JPX 公告，當月第3個 JPX 交易日09:00 JST 起，至少必須有前月末名單；此前可使用前兩個月末名單。第3個交易日由獨立 JPX 日曆推導，不能把休市日當工作日。接受月曆月底或該月最後 JPX 交易日作為來源日付；中旬日期、混合日期、過期名單、未來日期、重複代碼、欄名變動及格式異常都拒絕。2025–2027以外的日曆年份明確失敗並要求更新。

不會把今天下載的9月底名單改稱今日上市清單，也不把歷史名單視為歷史當時可得資料。每次保存原始 XLSX bytes、SHA256、抓取時間與來源URL；驗證失敗的來源也保存原檔及受控拒絕原因。網路失敗或來源過期不退回固定20檔，也不自動沿用已存舊檔。

來源：[JPX 東證上場名單與更新時程](https://www.jpx.co.jp/markets/statistics-equities/misc/01.html)、[官方 XLSX](https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx)。

離線驗證：`python -m unittest discover -s tests -p test_universe.py -v`。
