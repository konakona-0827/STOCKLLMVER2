# 正式 LLM 同條件 Paper 比較

此套件現在與正式 Skill 分支一起交付。匯出時從本機帳戶綁定取得策略選擇，使用本 checkout 的 `codex_cli.py` 及有界 CLI helper，不需要另一個同名 sibling checkout。原始行情、模型回覆、帳本及部署 registry 留在各電腦自己的 `research-runs/`，不經 Git 複製。

兩個版本使用目前正式選取策略、`gpt-5.6-luna`、`position_analyzer.build_request / validate / review_trades`、相同 schema 及獨立交易前審查。新版本額外載入 `skills/tw-paper-decision/SKILL.md`，是先前研究 Skill 的 Paper 決策適配版；不把只允許 RESEARCH 的研究提示直接塞進 BUY/SELL schema。

每個帳戶從 NT$100,000、零持倉開始；同一群益行情、每日買額 NT$20,000、最多 3 檔、每筆最多 999 股。佣金率 0.1425%、每委託最低 20 元、股票賣出稅 0.3%、0050 ETF 稅 0.1%、每邊 10 bps 不利滑價、依商品 tick 取整。部分成交累計同委託佣金與稅，T+2 才可重用賣出款，同日買入零股不可賣出。原實盤設定與帳戶不修改。

## 歷史重播

`prepare` 不讀歷史決策結果或未來損益來挑母體；從每個原始 request、行情快照及 manifest 驗證時間。歷史真實現金、持股、成本、賣出上限、買進計畫與額度全部移除，改由各自 Paper 帳戶供應。兩個帳戶後續只能因自己的決策與成交而分歧，不在每筆重置現金。

歷史禁止新網路搜尋，避免把今日新聞放進十月；兩個版本的工具政策一致。當前策略是在十月之後形成，所以十月只是開發重播，不是未見 OOS，也不是原線上搜尋流程的逐位元重執行。

```powershell
python -m research.llm_paper prepare --archive . --output research-runs/new-pair/cohort
python -m research.llm_paper export --output research-runs/new-pair/release
# 切換至 exported release 執行；所有輸出留在該 release/research-runs。
python -m research.llm_paper replay --cohort <cohort.json絕對路徑> --output research-runs/replay --strategy strategies/strategy_A_v2_2_calendar_aware_decisions.md --transport codex_cli.py
```

只有在決策完成後觀察到新買賣價，且含不利滑價仍穿過限價時才能模擬成交。歷史沒有五檔深度，明確使用共同的 NEXT_OBSERVED_CROSSING_SIDE_QUOTE_PROXY，不能宣稱重建了真實成交、排隊或未成交量。ROD 當日有效，不用決策同一棒成交，缺未來觀測保持未成交。LLM 失敗保留 ERROR，不能計作正常 WAIT。

為縮短完整重播，最多 6 個 CLI 呼叫並行、預取 3 個分別凍結的截點；只有完整 request hash 等於當時真實 Paper 帳戶輸入才重用。帳戶分歧時作廢並重算，作廢呼叫仍計入成本。每個截點只收到該時點資料，沒有把未來截點放進較早提示。API 額外帳單為零，會消耗 ChatGPT 方案／credits；沒有 API fallback。

## 前向資料與常駐程式

首日前向測試改用另存的 latest-funding protocol：AI 累計投入上限 NT$10,000、最低現金保留 NT$5,000、日額 NT$30,000、買單資金預留為價金加 max(1%, NT$20)，實際模擬佣金為 0.1425%、最低 NT$1。兩組模擬錢包仍各 NT$100,000，這不是券商真實餘額。使用 `profile` 從指定最新 checkout 與其綁定帳戶唯讀取得設定／AI 上限，記錄 commit、hash；不複製實盤啟用旗標。舊 v1 歷史成本與結果不變。

```powershell
python -m research.llm_paper profile --code-root <最新分支checkout> --output research-runs/new-funding
# forward 額外指定 --protocol <new-funding/protocol.json>
```

成交與因果行情證據、決策／委託及 checkpoint 現在以 SQLite transaction 同時保存，失敗回復記憶體帳戶。保留新鮮度樣本，拒絕未來本機接收時間與倒掛價。Pause 只停新決策，既有模擬委託持續觀察；收盤到期、晚到模型錯誤及每日比較報告自動保存。`latest-comparison.json` 是報告位置指標，歷史報告各存新資料夾；生成失敗保留錯誤並最多自動嘗試三次，可另外手動重跑。過去日期報告只讀到該日關帳，不混入隔日帳務。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File operations/SkillPaper.ps1 -Action Status
powershell -NoProfile -ExecutionPolicy Bypass -File operations/SkillPaper.ps1 -Action Report -Day 2026-11-26
```

`SkillPaper.cmd`／操作腳本只操作 Paper。Start/Resume 保留同版本帳戶、由 OS lease 防第二個 owner；沒有 Live 動作。沒有成交時報告明示缺少成交證據；NOT_STARTED、合成整合演練與真實前向結果分開，任何報告都不能授權實盤。

唯讀 collector 只建立 SKCenterLib、SKReplyLib、SKQuoteLib。使用 market=5 的盤中上市零股訂閱與五檔回呼，直接保存 nUp／nDown，不由前收猜測；五檔數量使用零股股數，不乘 1,000。商品分類來自證交所公司／基金 OpenAPI，日 K 來自證交所 STOCK_DAY，年度日曆用正確的 `date=20260101` 查詢並與已記錄的臨時休市校對。原始回應、雜湊與實際取得時間留存。

行情最後成交時間、五檔本機接收時間分開保存。群益五檔回呼沒有提供獨立交易所時戳，因此接收新鮮不代表已量得交易所至主機的完整延遲。雙邊價不一致、試撮、深度缺失／逾時、漲跌停未知、未完成日線或來源取得時間在未來時，不開新 Paper 倉；必要風險退出仍須等實際可用深度。

```powershell
python -m research.llm_paper capital-probe --output research-runs/new-probe --universe config/universe_tw.json
python -m research.llm_paper forward --output research-runs/forward --strategy strategies/strategy_A_v2_2_calendar_aware_decisions.md --transport codex_cli.py --sources <sources.json絕對路徑> --credentials-source <原.env絕對路徑>
```

實際部署用獨立 immutable release，不複製 .env、正式資料庫或 execution 套件。只有 collector 讀取原環境檔的群益登入設定；模型子程序會濾除券商及 API 金鑰環境變數。沒有 SKOrderLib、委託、刪改單或實盤授權修改。

程序盤外待命，下一交易日開盤前建立行情連線，09:10 首次撮合後才可 Paper 決策；2026-12-07 起依日期化規則改為 09:00。約每 30 分鐘一輪、兩個版本各一次主要決策，必要時各自再做原交易前審查。每交易日最多 40 次模型呼叫；額度、登入或模型錯誤停止，不改走 API。接收行情與模型工作分開，等待模型時繼續泵送 COM 事件，決策過期則拒絕套用。

模型另放入可終止的子程序，外層等待上限 150 秒，逾時只終止本次模型程序樹並限時清理；不等待可能被後代程序占住的 stdout pipe。錯誤訊息為空仍記錄原始 exception 類型。逾時沒有回報 token 時保持未知，不以零用量美化成本。這是針對本次歷史重播實際發現的子程序回收延遲所做的修正，原失敗紀錄仍保留。

`status.json` 是可重建狀態指標，真正歷史保存在 hash-chain、append-only `paper.sqlite3` 及逐次請求、回覆、原始快照。不同版本另建 release 與帳戶；原始碼改變拒絕沿用舊帳戶。OS lease 防同資料夾雙重啟動，checkpoint 保留現金、部分成交、成本、交割、未結委託及累積風險。

停止新工作時，將該執行目錄的 `control.json` 設 `active=false`；終止程序設 `stop=true`，保留 version 與 mode=PAPER_ONLY。控制變動及未套用的模型工作留在日誌。回退使用原 release 與原帳戶，不能重新解釋或覆寫舊決策。新年度若未建立經核實日曆則停止交易，不能把週一至週五當作完整行事曆。

## 限制

本輪未聲稱公司公告及公司行動資料已完整。日線為未還原上市市場資料，並非零股可成交價。歷史深度不能補造，CLI 模型目前權重也不能還原成十月權重。Paper 對手價與深度仍不代表真實排隊成交；策略增益須等前向標籤成熟後檢驗。LLM confidence 不當作勝率，因此沒有把它填入機率校準指標。
