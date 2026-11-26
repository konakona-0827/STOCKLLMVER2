# 正式 LLM 同條件重播與 Paper 前向測試

2026-11-25 完成。85 個歷史截點、兩個獨立帳戶的重播已完成；新版 Paper 程式已啟動，盤外待命。歷史結果顯示 Skill 版本虧損較少，交易樣本不足以認定有策略優勢。沒有啟用真實下單或改變實盤授權。

## 結果與適用範圍

| 指標 | 正式 LLM Baseline | 正式 LLM + Paper Skill |
|---|---:|---:|
| 起始資金／持倉 | NT$100,000／空倉 | NT$100,000／空倉 |
| 期末淨清算估值 | NT$99,431.75 | NT$99,918.00 |
| 淨損益 | −NT$568.25 | −NT$82.00 |
| Net Return | −0.56825% | −0.08200% |
| Maximum Drawdown | 0.57825% | 0.16200% |
| 完整平倉交易／勝率 | 1／0% | 1／0% |
| 每筆已平倉 Expectancy | −NT$137.25 | −NT$82.00 |
| Profit Factor | 0 | 0 |
| Sharpe，日報酬年化 √252、無風險率 0 | −8.5185 | −2.1798 |
| 買進／賣出成交筆數 | 2／1 | 1／1 |
| 模擬委託／拒絕／ROD 到期 | 6／0／2 | 4／1／1 |
| 期末持股 | 智邦 2345，2 股 | 無 |
| 期末未成交委託 | 1 筆賣單 | 無 |
| 有效決策／總截點 | 81／85 | 84／85 |
| ERROR，含傳輸錯誤 | 4 | 1 |
| 有效決策平均延遲 | 58.664 秒 | 59.984 秒 |
| 有效決策 p95 延遲 | 101.578 秒 | 95.078 秒 |

Skill 相對正式 Baseline 淨報酬為 **+0.48625 個百分點（+NT$486.25）**。這個 benchmark 指正式 LLM 比較組；沒有把此差異稱為超越台股大盤。此次未建立相同可成交模型的市場指數 benchmark，機器可讀結果將 market_index_relative_return 留為 null。

淨報酬包含現金、未交割應收款及持股的保守清算估值，扣假設退出成本；沒有在最後截點虛構強制平倉。Baseline 的智邦退出限價未穿過後續觀測到的對手價，保留未成交及持股。十個交易日與各一筆平倉的 Sharpe、勝率及 Expectancy 都很不穩定，不能推論可持續獲利。

這是**目前正式策略／模型／驗證與交易前審查流程，在共同離線 Paper 條件下重新推論**。策略為 `strategy_A_v2_2_calendar_aware_decisions.md`，模型 `gpt-5.6-luna`；沒有用規則選股器冒充 LLM Baseline。兩組使用 production `position_analyzer.build_request / validate / review_trades`，新組只額外附加 `skills/tw-paper-decision/SKILL.md`。原研究 Skill 限定 RESEARCH，不適合直接產生 BUY/SELL，因此建立了明確限定 Paper 的適配版本。

策略版本日期晚於十月資料；CLI 模型權重也不是十月快照。因此這是 development replay，不是未見 OOS，也不是原線上模型加即時搜尋的逐位元重播。歷史與本次前向比較皆採同樣的提供資料模式；歷史禁止新搜尋，沒有將今天新聞混入十月。

## 同條件控制與可重現證據

重播期間為 2026-10-08 至 2026-10-22，共 10 個交易日、85 個截點、170 份帳戶快照。候選母體依原始 request、manifest 與行情取得時間篩選，未讀歷史決策輸贏來選樣；缺原始請求時點、缺有效非試撮行情的截點列於 `cohort_summary.json`，沒有事後補造。

- 相同起始資金 NT$100,000、每日買額 NT$20,000、最多 3 檔、每筆最多 999 股。後續資金與持股依各自決策演進，不逐輪重置，也不沿用歷史真實帳戶。
- 相同群益歷史行情、截點、交易日曆、schema、LLM、交易前獨立審查及錯誤處理政策。
- 相同每委託佣金 0.1425%、最低 NT$20、部分成交累計同委託費稅；股票賣出稅 0.3%、0050 ETF 0.1%；每邊不利滑價 10 bps，依商品 tick 取整。
- 決策完成後的新行情才可撮合；含滑價的對手價須穿過限價。禁止同一觀測成交、同日買入零股轉賣、放空與重用未交割賣款；T+2 依交易日計算，ROD 同日有效。
- 歷史缺零股深度，兩組同用 `NEXT_OBSERVED_CROSSING_SIDE_QUOTE_PROXY_NO_HISTORICAL_DEPTH`。這是稀疏快照上的共同成交代理，無法證明排隊、深度、漲跌停隊列或真實委託一定成交。

`audit_replay.py` 已核對全部 **170 份快照、5 筆不同成交及170 個實際採用主要請求**：現金收支、T+2 應收款、持股數、成本與實現損益恆等式、可用現金、淨清算估值、成交晚於決策及限價約束全部通過。現金與庫存恆等式以保存成交獨立重算；精確請求重建則使用 frozen release 原 builder，並驗證 request hash 與模型 response ID。重播與前向版本的正式決策模組內容一致。

預取僅對各自凍結截點呼叫，完整 request hash 必須等於當時帳戶輸入才採用；帳戶分歧時重新計算，沒有將未來截點資料放入較早提示。總計 215 次模型呼叫，其中實際採用主要請求 170 次、採用審查 21 次、作廢預取及其審查 24 次；全部成本留存。

重播 cohort SHA-256：`4f484563aaeb8c99c9a7ad79d230c61d6c214ccd66d651600fe6ba7648eda2ba`。

正式策略 SHA-256：`ef2fe149783900d2ebf596ee2d6785938d6917fea5de1f36a8a9495de10234fe`。

## LLM 用量、失敗與延遲

| 用量範圍 | 呼叫 | 已回報 input tokens | 已回報 output tokens | 缺用量呼叫 |
|---|---:|---:|---:|---:|
| Baseline 採用主要請求及審查 | 96 | 4,513,813 | 240,987 | 3 |
| Skill 採用主要請求及審查 | 95 | 4,611,138 | 253,666 | 1 |
| 作廢預取及其審查 | 24 | 1,345,258 | 56,016 | 0 |
| 全部正式重播 | 215 | 10,470,209 | 550,669 | 4 |

已回報 cached input tokens 為 1,575,680。缺失用量沒有填 0；總 token 是**下限**。模型經既有 ChatGPT CLI／方案執行，未走 API fallback；額外 API 帳單記為 US$0，方案 credits 或訂閱經濟成本未知。相容性、初始 smoke 與新版 worker smoke 是額外建置驗證，未混入上表正式重播用量。

錯誤明細保留在原始 `replay_summary.json`：Baseline 有一筆 UNKNOWN_OR_DUPLICATE_SYMBOL schema 錯誤；10/21 的 Baseline 三次及 Skill 一次呼叫出現傳輸／回收異常，舊錯誤格式器在 exception 訊息為空時又拋 IndexError。依逾時設定、程序等待路徑及檔案時間，診斷為逾時後的子程序回收延遲；原底層 exception 類型未完整保存，無法逐筆完全還原。原始記錄仍是當時的 ERROR，沒有追補為交易或 WAIT。

四次缺完整時間與 token 的呼叫是 call-0173、0175、0178、0179。依 started_at 與 result 檔寫入時間約耗時 1,364–1,410 秒；這是檔案時間近似值，不是精確 RPC latency，亦不包含在有效決策延遲平均。不能只引用約一分鐘的成功延遲而忽略這些失敗。

前向版本已修正空錯誤訊息並採**模型子程序外層 150 秒 wall timeout**，只終止本次擁有的子程序樹，清理另有限時，避免後代程序占住 stdout pipe 導致長時間回收。新版 worker 已用同一 `gpt-5.6-luna` 實際成功呼叫，約 8.219 秒；逾時安全分支也有測試。這項修正未回寫舊重播。

Probability Calibration 保持 null：LLM confidence 不是交易成功機率，不能拿來計 Brier／ECE，也不能把 Monte Carlo 情境成功率當實盤勝率。本次沒有足夠成熟的獨立機率預測與交易標籤。

## 補齊的行情及 Paper 部署

已實際驗證群益 SKCOM 唯讀行情：20 檔均取得實際 nUp／nDown，market=5 零股訂閱成功；10 檔五檔回呼成功，保存並使用第一檔買賣價量，數量為股、不乘 1,000。沒有聲稱保存完整五檔撮合佇列。最後成交的交易所時戳與深度的本機接收時戳分開；深度回呼沒有獨立交易所時戳，因此無法據此量得完整端到端延遲。[群益零股五檔介面](https://www.capital.com.tw/apiforum/docs/atlObjFunction/domesticQuote/realTimeQuotesForOddLot/getQuoteBest5WithMarketNo)、[SKSTOCK 報價與漲跌停欄位](https://www.capital.com.tw/apiforum/docs/atlObjFunction/domesticQuote/delayedQuoteByNo/getStockByMarketNoAndNo)。

20 檔為 19 檔上市普通股及 0050 ETF，商品分類以[證交所公司主檔](https://openapi.twse.com.tw/v1/opendata/t187ap03_L)與[基金主檔](https://openapi.twse.com.tw/v1/opendata/t187ap47_L)確認；每檔補齊 22 根完成日 K，最新為 2026-11-25，供 11/26 使用。日 K 為[證交所 STOCK_DAY](https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=20261101&stockNo=0050)，是上市市場日線，不能當作零股可成交價，也未完成公司行動還原。

年度日曆改用正確的[2026 年查詢參數](https://www.twse.com.tw/rwd/zh/holidaySchedule/holidaySchedule?response=json&date=20260101)，與既有臨時休市紀錄交叉檢查。原始回應、hash 及實際取得時間全部保存。失敗來源下載嘗試也保留；已加入連線及總時限，沒有以 Yahoo 非官方即時價補缺。

實際部署為獨立 frozen release：

- 版本：`9378a06191bd544bb960`；PID：`26448`（觀測時）。
- 程式已常駐，`active=true`、`stop=false`，狀態 `ARMED_WAITING_NEXT_SESSION`。
- 下一輪為 **2026-11-26 台灣 09:10／日本 10:10**；08:50 台灣時間後建立唯讀行情連線，再等有效零股行情與深度。
- 兩個 Paper 帳戶各 NT$100,000，目前均無持股、未成交委託及前向成交。當日已收盤的群益快照只作連線驗證，未當作即時成交。
- 約每 30 分鐘一次；每交易日最多 40 次主要／審查模型呼叫。新鮮度：成交時戳 ≤120 秒、深度接收 ≤10 秒；決策過期拒絕提交。
- 持股與共同排名候選優先訂閱最多 10 檔深度；資料／深度／實際漲跌停缺失的商品不開新倉。兩個比較組使用同一行情框架與成交引擎。
- 僅建立 SKCenterLib、SKReplyLib、SKQuoteLib；不建立 SKOrderLib、不載入憑證、不呼叫委託功能。模型子程序移除券商及 API 金鑰環境變數。
- release 沒有複製 `.env`、正式資料庫或 execution 套件；collector 唯讀取得原 `.env` 群益設定。正式執行設定與授權機制沒有變更。

目前是盤外啟動驗證，**尚未驗證首個前向交易日的決策與成交結果**。程式需保持主機運作、網路及登入可用；本次沒有設定開機啟動或 OS 服務。

執行位置（均在專案內被忽略的 research-runs）：

```text
research-runs/llm-paper-20261125/forward-release-v3/
  forward-worker.pid
  forward-worker.stdout.txt
  forward-worker.stderr.txt
  research-runs/forward-live/status.json
  research-runs/forward-live/control.json
  research-runs/forward-live/paper.sqlite3
  research-runs/forward-live/model_calls/
  research-runs/forward-live/quotes/
research-runs/llm-paper-20261125/forward-sources-v4/sources.json
research-runs/llm-paper-20261125/replay-release-v2/research-runs/full-replay/
```

停止新工作：在**該 v3 執行目錄**的 `control.json` 將 `active` 改為 false，保留 version 與 PAPER_ONLY。結束程序將 `stop` 改為 true。不能刪除資料夾重置帳戶。先前 v1、v2 均已透過控制檔停止，保留版本與空帳戶歷史；不是覆寫同一場實驗。回退使用原 release 與原帳戶，新原始碼必須另建版本。`paper.sqlite3` 有 append-only trigger、hash chain、唯一時段 claim 與可回復 checkpoint；狀態 JSON 是指標，不是不可變歷史。

## 測試、修改及未完成項目

完整測試 **356／356 通過**，其中新增正式 LLM／Paper 測試 25 項：unit、integration、未來資料洩漏、資產會計、Paper 安全、部分成交費用、T+2、ROD、深度不重用、真實帳戶上下文移除、雙組請求一致性、來源時點、hash-chain、checkpoint、模型硬逾時。另已完成 Skill 格式驗證、真實群益唯讀連線、隔離 release 啟動及新版模型 worker smoke。詳見 `test_results.txt` 與 `audit.json`。

CI 已新增新模組／Skill 路徑及測試，保留 Windows／Ubuntu 離線 gate；本次是本機驗證與本機 Paper 部署，沒有宣稱 GitHub Actions 已執行或已 push。正式下單鏈與原已修改的實盤設定／資料保持原狀。

仍需後續證據：

1. 首個盤中交易日確認 callback 新鮮度、候選排名、模型工作、ROD／部分成交及收盤 checkpoint；用前向保存的同一行情比較兩組，保留 ERROR 與未成交。
2. 先凍結評估期間與退出標籤，再累積獨立前向樣本；不得依本次收益調參後回稱 October 為 OOS。前向資料成熟前不擴大資金、不啟用實盤。
3. 補獨立可成交市場 benchmark、公司行動、停復牌／新臨時休市及新聞發布時點資料；歷史深度不能事後造出。跨 2027 年前須更新核實日曆；未核實年度不交易。
4. 若輸出機率預測，先定義固定持有期／成本後事件，再用時間外與 walk-forward 標籤評估 Brier／ECE／可靠度；繼續把模擬條件機率與真實勝率分開。
5. 在未見區間分別比較 block bootstrap、regime／波動條件、流動性及滑價敏感度，避免同時改多個因素而無法歸因；新版本另開 release、保留原帳戶。

## 交付檔案

- `comparison.json`：補齊成本缺值語意、正式 Baseline 相對報酬、延遲及部署快照。
- `audit.json`、`audit_replay.py`：實際 170 份快照核對證據及唯讀重跑程式。
- `replay_summary.json`：原完成摘要原樣複製，保留錯誤；`replay_protocol.json`、`replay_source_manifest.json`、`replay_cohort_manifest.json`：凍結條件與雜湊。
- `cohort_summary.json`：母體篩選／排除原因；`test_results.txt`：356 項測試原始輸出；`CHANGES.md`：修改紀錄。

原始每次模型請求、回應、行情、完整帳戶快照及來源檔留在本機 research-runs；本報告只附彙總、hash 與重現路徑，不覆寫既有歷史結果。
