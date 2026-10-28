# 日股第一階段架構

2026-10-28，分支 `codex/japan-support-desk-phase1`，由台股 `7d036a6` 分出。沿用可驗證的固定狀態機及雙代理，避免重寫已具因果測試的核心；市場契約、行情來源與決策政策分開。

0.5.0補齊原要求的尋標層：`JPX官方動態母池 → 輪替有界Yahoo行情 → 固定支撐預掃 → SEARCH agent → 決策研究員／反證員 → 固定決策與跨日帳本`。0.4.0和台股原分支只有固定池掃描及兩角色研究，不能稱為agent自主發掘，這是先前未完成的範圍。

| 層 | 實作 | 責任 |
|---|---|---|
| 上市母池 | `universe.py` | JPX月末名單、來源日期／SHA256、普通股篩選、分層輪巡 |
| 暫時行情 API | `japan.py` | 正規化代號、來源身分與資料驗證、獨立 JPX 日曆、原始 JSON 稽核 |
| 市場契約 | `contracts.py` | 由 market 選 TSE/JPY/JST 或舊 TWSE/TWD；兩種成交值欄位與門檻不混用 |
| 搜尋 | `support.py` | 因果支撐狀態機、固定下緣、證據日期、流動性與距離排序 |
| SEARCH | `discovery.py` | 獨立模型從有效預掃候選提案、重排或不選；引用同標的證據 |
| Head | `desk.py` | 程式固定分派、整對呼叫預算、快照綁定及稽核 |
| Researcher / Reviewer | `agents.py`、`providers.py` | 分別研究與反證；同快照不同上下文，反證可否決 |
| 決策 | `decision.py` | 可考慮／等待／不交易，歷史阻力與情境風險報酬，硬閘門優先 |
| 跨日 | `ledger.py` | 凍結區間、修訂阻擋、失效不可復活、倒退資料不覆寫 |
| 發布 | `observation.py`、`storage.py`、`reporting.py` | 帳本提交前重算決策、完整發布、完成標記及 latest 指標 |

日常入口預設日本；legacy CLI、TWSE adapter 與原 102 項測試保留作相容性基準。日股資料必須明確 market=JP，不能只把台股代號改成 `.T` 就繞過市場契約。

未來替換資料來源的接點是 `observe(builder=...)`。builder 保持 `build_snapshot(symbols, cache_dir, *, months, timeout, progress)` 介面，回傳 schema_version=1 的 JP bundle；切換券商行情不應改動固定支撐、角色契約或決策閘門。需重新驗證調整口徑、單位、日曆、資料取得時間與來源證據。此接點只處理行情，不授予帳戶或訂單能力。

每個 agent packet 涵蓋當次截止、規則、角色任務、可見日線、日曆與背景證據；任一改動換 hash。兩角色不同上下文不等同不同模型或統計獨立。模型回覆不能修改價位、生成虛構勝率、補上不存在的資料，或移除資料閘門。

SEARCH packet另綁完整母池hash、所有eligible候選、實際提供池、研究名額與原snapshot。動態core保留 `discovery_required`、`source_universe_hash`；派工入口與決策層均重驗提交，非SEARCH選定標的不能形成CONSIDER。反證員看原行情，避免被SEARCH敘事錨定。總呼叫額度包含搜尋；合法空選和來源／模型失敗分開，不回退數值topN。

輪巡cursor在成功發布後以revision CAS提交；搜尋／設定失敗不前進，名單換月從last_symbol接續，舊月份晚完成不覆寫。無agents的NUMERIC_ONLY可推進數值掃描，但不能宣稱完成agent研究。一次掃描100/3695只是部分覆蓋，完整輪巡需37次執行。

跨日 SQLite 在 transaction 內產生 summary，發布 callback 掛上帳本結果後重新套用決策政策，再寫報告及最終 digest；發布失敗回滾。歷史修訂、倒退資料與失效 episode 都不能被模型重新批准。報告成功與否、資料是否完整、技術型態及可交易性是不同檢查。

目前角色相當於 Head、SEARCH、RISK 的初版；EXIT 只先保留失效條件研究，沒有持倉執行。原貼文的錢包追蹤、熱度確認與加密貨幣下單路徑不直接搬到日股。帳戶 5% 停工待未來可信帳戶資料與獨立風控服務設計後才實作。
