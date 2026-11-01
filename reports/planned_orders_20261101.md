# 計畫限價委託：2026-11-01 交付

分支：`codex/planned-limit-orders`。以既有 A／B 研究版本為基礎，實作「願意在某價格買進／賣出」與委託生命週期。沒有修改真實帳戶、現行排程或啟用實盤；原有尚未提交的工作區修改不包含在本次提交。

## 實際問題與改修

現有 `CapitalOddLotExecutor.send_limit_order` 本來就送限價單，但 `execution.risk_guard._choose_limit_price` 會用當下 ask／bid 取代 suggested_price；原本的 buy_plan 主要是成交後目標／失效價計畫，沒有獨立等待中的進場計畫。

- `AdviceIntent.pricing_policy` 新增 `AT_QUOTE`／`PLANNED_LIMIT`；舊模式與預設行為不變。
- `execution.advice_adapter` 保留顯式定價模式；`RiskConfig.allow_planned_limits` 預設 false。因此舊實盤入口不能默默啟用新的計畫委託，未知模式也會拒絕。
- `quant_research.limit_orders.PricePlan` 保存價格、數量、目標／停損、理由、建立／到期時間、策略／模型版本及失效條件。限價用普通股跳動單位驗證，限定 1～999 股；不接受未驗證 ETF 規則。
- `LimitOrderBook` 重用既有 Paper 帳戶 schema、成本設定與獨立 RiskEngine；新增 plan／order／fill／event／quote tick 表。正式 execution／analysis DB 會在寫入之前被拒絕，必須使用獨立實驗帳本。
- 建立計畫不增加持倉；ACKNOWLEDGED 只預留資金。成交採後續、較新的 LIVE 零股 bid／ask 與明確股數單位，且每個報價快照的已顯示深度不可重複使用。
- 部分成交只更新已確認股數，剩餘委託繼續等待；手續費採同一委託累計成交額計算，避免每個部分成交重收最低費。
- SQLite `BEGIN IMMEDIATE` 保護同時送出計畫的資金預留；等待中買單也納入資金、持倉數與產業集中度檢查。每個標的同時最多一張等待單。
- 取消請求為 CANCEL_PENDING，收到獨立模擬取消確認才釋放未成交部分資金。取消期間仍可能成交；已成交部分不會因取消而消失。
- 當日計畫到期只在 Paper 帳本標記 EXPIRED，不推定券商已取消。冪等計畫 ID、tick ID、事件時間順序與原始輸入紀錄可防止重啟後重送、倒寫歷史或更換同 ID 的行情。
- `quant_research.planned_cycle` 接上 A／B Scanner；量化證據不足仍不建立 BUY。初版願意買價為 `min(前一日收盤, MA5) × (1-min(0.5%, 0.25×ATR比率))`，向下取合法 tick。這是可檢驗的基準公式，不代表最佳價格或已證明獲利。
- 進場計畫的盈虧在指定買價、預期成本與停損風險下重新檢查，不要求當下 ask 已低於限價。出場也保存限價計畫；停損條件觸發不保證限價賣單可成交。
- 新聞／趨勢失效可透過 `invalidated_symbols` 或具來源與發布時間的 BLOCK 事件取消等待中的買單；已有持股則產生退出計畫，仍須獨立通過風控與禁止零股同日賣出的限制。

## 使用與報價格式

```powershell
python -m quant_research.planned_cycle --bundle quotes.json --output data/planned_paper/experiment_001
python -m quant_research.limit_demo --output reports/planned_order_demo_20261101.json
python -m unittest discover -s tests -p test_planned_limit_orders.py -v
python -m unittest discover -s tests -v
```

Bundle 為 `{at, tick_id?, quotes: [...], sessions: [...], events?: {...}, invalidated_symbols?: [...]}`。
每個 quote 須有 symbol、quote_timestamp（含時區）、quote_status=LIVE、last_price、bid／ask、bid_size／ask_size、lot_type=INTRADAY_ODD_LOT、book_size_unit=shares；試撮合或超過 120 秒者不可送新計畫或成交。sessions 由呼叫端提供驗證過的交易日，用於持有期間及賣出 T+2 交割。A／B 各自 SQLite，版本／成本改變須另建實驗。

目前沒有新增任何 LLM 呼叫。等待期間只由程式處理報價、風控、失效及成交事件，不會為每次行情輪詢重問模型。

## 可重現的實際程式驗證

`planned_order_demo_20261101.json` 使用明確標為 SYNTHETIC_TEST_ONLY 的價格序列，在臨時 Paper 帳本執行：

| 階段 | 可用帳戶現金 | 等待單預留 | 已確認持股 |
|---|---:|---:|---:|
| 現價 100，接受限價 98.5 買 50 股 | 100,000 | 4,945 | 0 |
| last 觸及 98.5，但 ask 仍 99 | 100,000 | 4,945 | 0 |
| 新 ask 98.5、量 20，確認部分成交 | 98,010 | 2,955 | 20 |
| 新 ask 98.4、量 30，再確認成交 | 95,058 | 0 | 50 |
| 次日限價 102 賣出確認，款項未交割 | 95,058 | 0 | 0 |
| T+2 款項到帳 | 100,122 | 0 | 0 |

這只是功能驗證，122 元為合成行情的模擬損益，**不是策略歷史／前向收益**。兩次買進部分成交合計只計一次 20 元最低費；賣出計手續費 20 元與證交稅 16 元。

新增測試涵蓋：保留限價、舊實盤預設拒絕、新報價後才成交、last 觸價不成交、部分成交、缺量／過期行情、取消競態、失效、到期、重啟冪等、計畫不可變、等待單產業上限、零股同日賣出限制、T+2、tick、模型證據不足、A／B 串接、時間倒序、同 ID 輸入衝突、同時送單的原子資金檢查、正式帳本保護。完整結果在同目錄的測試輸出檔。

最終新增測試 **19／19 通過**。為避免把工作區原有未提交修改當成依賴，另從程式提交 `0a9a724` 以 `git archive` 匯出乾淨副本、移除 TRADING_STATE_ROOT 綁定後執行全套測試：**337／337 通過（43.992 秒）**，輸出見 `planned_order_clean_regression_20261101.txt`。工作區較早一輪為 344／344；數量差異包含原有未提交測試，Push 的版本以乾淨副本結果為準。驗證全程未連券商或發送交易。

## 範圍與限制

- **本次接入的是獨立 A／B Paper 計畫流程，尚未接入正式券商自動送單／撤單或 GUI。**既有實盤仍採原模式，沒有自動切換；後續若要導入，須另行授權並接上券商精確委託序號、回報與對帳。程式沒有提供一個可誤開實盤的 CLI 旗標。
- 成交模擬依未來觀察到的可成交 book 與量，無逐筆隊列資料，仍是 book proxy；不宣稱與交易所真實排隊／成交機率相同，也不把價格到了當作一定成交。
- 可使用 `LimitOrderBook.create/submit/request_cancel/confirm_cancel/process/snapshot` 手動測試任意經風控接受的計畫；CLI 只從量化通過候選產生新進場，不會降低 A／B 原樣本門檻湊交易。
- 當日未成交不自動追價或跨日續掛；下一日必須重新評估並建立新計畫。未實作市場單、整股單、跨日原生委託或保證停損。
- 成本為既有研究設定（預設最低費 20 元），不是新查證的實際券商折扣。停利／停損評估保留預期滑價，實際限價模擬成交價格不超過買價上限或低於賣價下限。
