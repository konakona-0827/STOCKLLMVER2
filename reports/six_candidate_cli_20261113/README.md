# 六檔真實 CLI 研究與執行介面驗證

2026-11-13；本文時間均為台灣時間 UTC+8。基準版本 `a57635f`。

**六檔都完成真正 Codex CLI 研究，沒有雙方共同核准的可買進計畫。實際券商登入、帳戶及上市／上櫃零股行情可取得；0050 一股 BUY／SELL 的真實 SDK 結構也可建立，但尚未驗證真實委託受理、成交與回寫。此次實盤送單、撤單及操作上的模擬交易皆為 0。**

佣金採使用者指定的群益官方無已知折扣標準：0.1425%、電子交易每筆最低 NT$1；NT$20 繼續作為每筆資金預留。後續依使用者明確指示，把帳戶最低保留金額由 NT$10,000 改為 NT$5,000，專案設定與綁定正式帳戶均只改這個欄位；淨利、報酬風險、配置、損失風險及研究門檻沒有更動。[群益官方說明](https://www.capital.com.tw/event/stock/oddLot/default.asp)

**最後重查：12:44:47 的正式帳戶預算為 READY、買進可用額度大於 0，六檔帳戶門全部 PASS；12:45:11 完成的報價也全部 LIVE。使用可買資金的新研究仍為五檔 WATCH、益登 REJECT，沒有雙方共同批准的 BUY。先前「零預算」擋點已解除，沒有以零預算代替本次選股結論。**

## 原六檔與當日重查

「原六檔」來自 11/12 08:30、日線截至 11/11 的數值篩選。重跑時使用真正 CLI 研究員與獨立反證員，但 NT$10,000 為歷史資金情境，沒有當時的真實帳戶／即時報價，因此不能作為今日委託。原數值合格不等於模型核准。

當日研究使用截至 11/12 的完成日線。11:07 真實券商資料搭配的四次 CLI 為調整前紀錄；最低保留金額改為 NT$5,000 後，12:42:49 重新讀取六檔 LIVE 報價及 READY 帳戶，再跑四次真正 CLI。因六檔均已在程式層被擋住，補做的研究卡明確標記為診斷用，`selected_routes=[]`，不能從診斷意見產生 BUY。下表當日結論採用保留 NT$5,000 的最後一組研究。

| 股票 | 原凍結資料：研究員／反證員 | 當日：研究員／反證員 | 當日程式擋點與具體原因 |
|---|---|---|---|
| 1402 遠東新 | WATCH／WATCH | WATCH／WATCH | `NO_SETUP`；27.5 低於 MA20 28.36，沒有目前成立的型態。原 27.05 回測路徑尚待出現，不能由曾有高點推定五日內可達。 |
| 1514 亞力 | REJECT／REJECT | WATCH／WATCH | `NO_SETUP`；84 低於 MA20 93.005，20 日約跌 17.65%。原資料的 101 目標所需反彈缺少五日期限內的充分支持；當日也沒有新合格路徑。 |
| 3048 益登 | WATCH／WATCH | REJECT／REJECT | `SCREENED_OUT`；20 日平均成交金額約 NT$95,848,245，低於原 NT$100,000,000 門檻。沒有放寬流動性要求。 |
| 4904 遠傳 | WATCH／RESEARCH | WATCH／WATCH | `NO_SETUP`；108 低於 MA20 111.55，原 108→115 復甦假說尚未確認，原資料也只有一方認為值得研究。 |
| 4966 譜瑞-KY | WATCH／WATCH | WATCH／WATCH | `NO_SETUP`；391 低於 MA5 397.6、MA20 476.275，五日約跌 7.02%。未形成新的可批准價量路徑。 |
| 8210 勤誠 | WATCH／WATCH | WATCH／WATCH | `NO_SETUP`；601 仍低於 MA20 710.25，11/12 由當日高點 657 收至低點 601；近期反彈未形成目前的合格型態。 |

每檔的完整論點、反證、等待條件、證據 ID 與日線摘要在 [per_stock.json](per_stock.json)。兩個截點不同，1514 由原拒絕轉為診斷觀望不是買進批准；3048 的當日拒絕有明確流動性依據。

調整前另有共同帳戶擋點：12:08:13 的正式快照買進可用預算為 0，原因為 `BELOW_MINIMUM_BALANCE`、`NO_BROKER_HEADROOM_AFTER_RESERVE`。12:08:42 六檔均取得 LIVE 零股行情，當時獨立帳戶門為 `CURRENT_ACCOUNT_ALLOWANCE_PAUSED`。這是當時 NT$10,000 保留金額下的歷史紀錄；依使用者指示改 NT$5,000 後，12:44 的新帳戶門已全部 PASS。

程式使用 `GetBalance` 第四欄「當日可買進金額」，沒有用第二欄的一戶通帳面餘額冒充可買資金，也沒有再扣一次 `GetT3DueAmt` 交割清單。第三欄「可出金」與第四欄並非同一概念，即使這次數值相同仍分開保存；欄位順序已核對[群益官方 GetBalance 範例](https://www.capital.com.tw/apiforum/docs/atlObjFunction/capitalPay/getBalance)。本次差額的交割逐項對帳仍沒有完整證據，不宣稱已證明每一元差額來源。

## 真實跑過的範圍

| 層級 | 證據與結果 |
|---|---|
| 官方資料更新 | 隔離資料集含 65 個交易日、126,865 列；更新至 11/12。5 次新 HTTP、131 次命中快取，另查四項公司事件／行動官方來源；不宣稱歷史價格已取得完整公司行動認證。 |
| 真實 CLI 研究 | 四組各四次，共 16 次成功 CLI、48 個逐股意見；含保留 NT$5,000 的四次新研究，沒有 API 備援。 |
| 真實券商讀取 | 原 COM owner／帳戶路徑，上市 market 5、上櫃 market 6；六檔均成功取得 LIVE 報價。未呼叫送單或撤單方法。 |
| 研究後正式風控 | 調整後六檔帳戶門 PASS，但真實意見沒有形成雙方批准，當日程式也無可買計畫。原下單前複核模型、原正向委託風控及正式送單未進入，均標示 `NOT_REACHED`。 |
| 真實 SDK 結構 | 12:29:06 使用本機 SKCOM.dll 的 72-byte `STOCKORDER`，由正式送單共用建構函式建立 0050 一股 BUY／SELL；沒有登入或呼叫 `SendStockOddLotOrder`。 |
| 回歸測試 | 全套 700 項，新增 9 項；另加強既有 BUY 正向案例，從新橋接資料實際執行 `execute_advice_file`、原 `CapitalOddLotExecutor.send_limit_order` 與共用委託建構，只替換 SDK 邊界，核對固定股數、SUBMITTED、確認部分成交才回寫及防重送。結果見 [regression.txt](regression.txt)、[summary.json](summary.json)。隔離帳本與替身 SDK 只證明程式串接，不代表真實受理／成交。 |

0050 結構檢查的價格 `100.50` **僅是欄位測試值，不是即時報價或建議委託價格**，帳號也固定為不可送出的占位文字。它驗證真實 SDK 型別及參數相容性，不能證明券商會接受這張單。完整欄位見 [native_sdk_order_0050.json](native_sdk_order_0050.json)。

正式讀取期限修正後、保留金額調整前另做只讀查詢：12:35:55 資料中，0050、1514、4904、4966、8210 為 LIVE，1402、3048 因成交時間較舊標為 `LAST_KNOWN`，不可作即時執行依據。這組當時預算仍為 0，結果見 [postfix_broker_read.json](postfix_broker_read.json)。後來保留 NT$5,000 的最後一組重新查到六檔全部 LIVE、帳戶 PASS，見 [quotes.json](quotes.json) 與 [summary.json](summary.json)；不把之前報價當成永久有效。

## 找到並修正的流程問題

1. `LegacyRuntime.candidates()` 從錯誤模組匯入 `pending_exit`，真正接到原持倉上下文時會發生 ImportError。改為原函式實際所在的 `execution.exit_cancel`，增加呼叫真實 Runtime 方法的回歸案例。
2. 已批准固定計畫交給下單前模型時，候選的 `max_buy_qty` 原本仍為 0。現在提供同一個已審核的固定股數；沒有擴大股數或降低風控。
3. 第一組補充診斷卡漏帶實際取得的報價，模型因而報告缺即時行情。補入時間明確的零股報價、維持零條可批准路徑，再花四次真正 CLI 重跑。第一組保留作除錯及用量證據，不拿來作最後當日結論。
4. 外層 `Future.result()` 原本無限等待，SDK 的 `timeout=12` 只限取價階段。補上帳戶健康讀取 60 秒、行情讀取 `45 秒 + 每 20 檔一批 × SDK timeout` 的整體期限。預設每批 12 秒，六檔所在的單一市場請求為 57 秒；上市／上櫃分開請求。逾時或晚回覆後拒絕新動作，已排隊的委託也不能繼續執行；不自動建立第二個 COM owner、不自動重登或重送。
5. 關閉等待最多五秒即回報 COM owner 仍在停止；原 owner 未結束前保留 OS lease。不能以關閉介面推論券商委託已撤除。新增逾時、排隊動作、時間跳躍與 owner lease 的故障測試。
6. 原手動工具 `real_order_test.py` 遇拒單仍固定印 `REAL_ORDER_SENT=YES`。改按原執行器證據輸出 YES／NO／UNKNOWN、是否核實受理及確認成交量；拒單或未確認不再回傳成功結束碼。
7. 依使用者指示只將 `min_available_to_buy_twd` 改 NT$5,000，重查正式快照及重跑六檔 CLI，確認帳戶門已解除。完整的帳戶設定修改前備份與確認後金額留在本機，沒有公開個人餘額。

## 行情時間缺口

當日第二組研究後，11:09:53.557480 開始的行情請求直到 12:06:21.982212 才完成。**原因尚未確定**，不推定是券商、網路或電腦休眠。當時帳戶已過期、報價也不能用，原防護正確擋住。

之後以父程序 45 秒上限的獨立只讀程序重新查詢成功，取得 12:08 的新帳戶與六檔 LIVE 報價。新增的正式讀取期限是對無限等待問題的對策；不能保證作業系統暫停時程式仍會運作，也不代表已查明這次缺口原因。

## 原手動買單與正式 FLOW 的關係

原專案的 [real_order_test.py](../../real_order_test.py) 來自 `91015e4`，可以手動測一筆券商委託。它直接呼叫 `CapitalOddLotExecutor.send_limit_order()`，不經 FLOW 的雙方研究、正式預算／經濟風控及持倉帳本回寫，且明確輸出 `POSITION_UPDATED=NO`。因此不能把這支程式接成 FLOW 的捷徑，也不能用一筆手動成功單宣稱整個 FLOW 完成實盤驗收。

正式路徑仍為 `ExecutionBridge` → `LegacyRuntime.execute()` → 原 `BrokerRuntime` → `pipeline_hook` → `execute_advice_file()` → 原 `CapitalOddLotExecutor` → 券商回報／正式帳本。此次只讓委託結構建構共用，沒有新增跳過風控的送單入口。

程式開發與送單是兩件事：本次未代使用者呼叫真實金融交易；實盤送出須由使用者自行操作。要認定正向驗收完成，仍需取得相同正式流程下的受理識別、券商核對、實際成交及帳本結果；目前沒有這些證據。

## 用量與重現

16 次 CLI 合計輸入 314,334 tokens（其中快取命中 100,224，已包含在輸入數內）、輸出 23,270 tokens；推理輸出 6,699 亦為工具回報的用量子項。使用既有 Codex 方案額度，額外 API 帳單 USD 0；不把方案額度稱為無限免費資源。

- `calls/reserve5000_*`：保留 NT$5,000、帳戶門 PASS 的最後當日研究。
- `calls/current_with_quotes_*`：含真實報價、保留 NT$10,000 的調整前研究。
- `calls/frozen_replay_*`：原六檔的歷史資料／資金情境研究，不可執行。
- `calls/initial_diagnostic_*`：第一組漏帶報價的除錯證據。
- [quotes.json](quotes.json)：公開市場報價與時點，不含帳號或全帳戶持倉。
- [summary.json](summary.json)：機器可讀摘要及封存時程式碼雜湊；不宣稱四組研究均使用完全相同程式版本。

若模型回答含有個人帳戶金額，公開副本以「帳戶金額已移除」遮蔽並附未遮蔽來源雜湊；本機原始回答保留完整數字。少數診斷意見另提整股報價需求，這不是正式零股送單的額外門檻；正式執行資料仍以 market 5／6 的有效零股買賣價為準。本次具體擋點以無合格型態／流動性與雙方未批准為準。

```powershell
# 真正 CLI 研究及券商只讀查詢，沒有送單／撤單模式：
python -m operations.review_flow_candidates `
  --dataset local_data/six_stock_cli_20261113/dataset `
  --output local_data/my_six_stock_review

# 使用本機真實 SDK 建構欄位，不登入券商：
python -m operations.inspect_capital_order `
  --symbol 0050 --qty 1 --price 100.50 `
  --output local_data/native_sdk_structure.json

python -m unittest discover -s tests -v
```

實際帳戶快照、帳號、餘額明細、完整持倉及研究上下文留在忽略的 `local_data/six_stock_cli_20261113/`，沒有推到遠端。公開報告只保留必要擋點與去識別結果。
