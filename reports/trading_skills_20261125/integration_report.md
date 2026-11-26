# 開源交易 Skills 台股整合研究

研究日：2026-11-25。既有 checkout：`STOCKLLM`，`codex/tw-selection-lab`，起始 commit `5f8cd1c`。這次完成原始碼盤點、原測試、台股獨立實驗、Paper 前向介面、研究 Skill、CLI 比較及安全驗證。沒有替換正式策略、修改實盤授權、連線券商下單、推送或部署遠端 CI。

## 判斷與適用範圍

採用「先凍結證據與計畫，再做模擬、配置及風控」的流程，改寫為台股 Paper 研究模組。現有資料與本輪結果不足以支持正式替換或宣稱提高真實勝率。既有正式程式、策略來源及研究 Baseline 已另存原檔與雜湊。

本輪可重現的量化 Baseline 是既有 `selection_lab` 的 20 日動能排名；它不是正式 LLM 策略。正式 LLM 含盤中行情、帳戶上下文及工具呼叫，不能把事後日線排名回測冒稱它的收益。正式策略的完整同條件重播與成熟前向比較仍未完成，列為升級前必要工作。

## Phase 1：來源、依賴與原測試

| 專案 | 固定 Git commit | 授權 | 盤點範圍 | 原測試 |
| --- | --- | --- | --- | --- |
| [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill/tree/3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd) | `3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd` | MIT | 85 個 tracked 檔、11 Skills、24 Python 檔 | 315 / 315 通過 |
| [trader-skill](https://github.com/Jakeschincariol/trader-skill/tree/5189e79b9a480755a3733ac9713f5353981dbe18) | `5189e79b9a480755a3733ac9713f5353981dbe18` | MIT | 94 個 tracked 檔、11 Skills、24 Python 檔 | 175 / 175 通過 |

Clone 保存在工作區上層 `../.research-upstream/`。`audit.py` 讀取每個 tracked 檔並保存來源 SHA256；完整讀入所有 SKILL、Python、JSON 設定及測試，解析 AST、函式、測試、I/O 與 URL。兩份 `inventory.json` 與原測試全文另存本報告目錄；完整原始碼閱讀檔 `source-review.txt` 位於本機研究資料夾。不是僅閱讀 README。

兩套 Python 工具與測試使用標準函式庫，沒有 requirements／pyproject 套件清單。選用的 PTR PDF 轉文字依賴系統 `pdftotext`，本機未安裝，原測試主要使用已抽出的文字，因此測試全過不代表實際 PDF 擷取可用。Alpaca MCP、Claude Desktop、SEC User-Agent 是特定功能的外部設定；本次未安裝或接線。新研究核心也只用標準函式庫；CLI pilot 沿用現成 transport 及其 OpenAI Response 型別套件。

MIT 全文與來源版本保存在 `research/trading_skills/THIRD_PARTY_NOTICES.txt` 及原專案授權備份。MIT 程式授權不代表行情資料可任意再散布；本報告只提交彙總與雜湊，原始行情留在被忽略的 `research-runs/`。

## Phase 2：模組比較與採用決策

| 模組 | 原始實作與限制 | 與目前 STOCKLLM 比較 | 決定及本次範圍 |
| --- | --- | --- | --- |
| trader-scanner（日內版） | Nasdaq Trader 母體；Yahoo spark／chart；20 日收盤突破、旗形、MA20 收復、Wilder RSI、ORB 二階段確認 | 現有 `quant_scanner.py` 是即時快照動能、量與價差分數，已明說不是歷史突破；selection_lab 有獨立日線特徵 | **改造**：`setup_tags` 保留五種日線形態；同一凍結排名前 3 名做篩選，不以排除後遞補美化結果。沒有移植美股全市場掃描 |
| trader-entry（日內版） | 完整 15 分鐘開盤區間、首次收線越過、量比；美東入場窗口；Yahoo 非官方分 K | 正式系統有策略證據及即時資料檢查，日線與快照不可混充完整分 K | **重新實作**：`entry.py` 以台股時區、日曆及已完成連續分 K 判斷；本輪日線回放未啟用 ORB，真實分 K 前向測試待補 |
| trader-exit（日內版） | OR 低／中點、百分比或 ATR 停損，R／百分比目標，固定平倉時間，美分取整；多頭為主 | 現有固定基準有 +10% / -5%，LLM 可提供目標及失效價，另有持倉檢查 | **改造**：獨立協議固定 +2% / -2%、次日開盤、持有 5 日；台股跳動單位、不利滑價、跳空較差成交與同棒停損優先。這是研究條件，未改正式出場策略 |
| trader-timemachine（日內版） | 60 日五分 K、78 棒完整美股日；相近時段 ±2 棒獨立抽樣；10,000 路徑；歷史 ORB 重播；無費稅、帳戶及校準 | selection_lab 是逐筆障礙情境，strategy_experiment 有獨立資金，但沒有這套 Monte Carlo | **重新實作**：日線 IID、3 日 moving-block、狀態與波動條件抽樣；費後情境頻率、帳戶回放、前序校準。保留上游原測試作參照，日線 IID 不冒稱原 78-slot 演算法的逐位元重現 |
| trader-news（兩版） | Yahoo RSS、SEC Atom、標籤、去重與來源連結；部分時間未知仍可出現 | 現有 web_research 工具需保存搜尋與來源，日線研究沒有完整歷史公告 | **改造**：新 Skill 要求原文與來源；`evidence.py` 強制發布及取得時間。未移植 RSS 為台股即時事實源；MOPS／交易所公告的完整擷取器待補 |
| trader-risk（兩版） | 格式、現金、持股、名單、日額度；日內版增加停損風險、R 比、每日損失、窗口及 PDT。順序假設通過訂單已成交 | 現有 risk_guard、執行帳本與券商對帳更接近真實委託／成交及未結保留，不可被通用 JSON 風控取代 | **重新實作必要研究限制**：每筆名目風險、每日買額、持倉數、現金、行情與深度；不移植 PDT 或實盤通道。尚未加入整套上游每日損失、相關性／產業曝險模型 |
| trader-budget（兩版） | 投資版按目標權重、現金保留與上限配置；日內版取停損風險／持倉／委託／現金四者最小股數；浮點與美元／Alpaca 格式 | 現有群益零股帳戶有額度、未結保留及交割約束 | **改造**：Decimal 費稅、最小費用、股數與名目停損風險；獨立帳戶、T+2 保留、部分退出保留成本。未核實實際帳戶手續費，配置不是券商可買額 |
| trader-analyst（兩版） | SEC XBRL、季報／TTM、Q4 衍生、年比及財報延遲提示；`periods()` 選最新重編值 | 台股需 MOPS／IFRS 科目與申報時點；不能把美國 companyfacts 直接當台股基本面 | **重新實作時點介面**：同期間只取決策前已發布且已取得的版本；新 Skill 區分財報期間與公開時間。財報計算與 MOPS 科目映射尚待資料接入 |
| trader-alerts（兩版） | 價格／時間提醒、去重、美東日曆，日內版追加平倉提醒 | 既有 scheduler、行情時效與 GUI 已有對應流程 | **不直接採用**：沿用現有執行機制；新實驗只抽離日期化市場規則，不新增排程 |
| trader-broker（兩版） | Python 產生票券、fingerprint、記錄逐筆 yes 及成交；真實工具呼叫由 Skill 引導外部 Alpaca MCP | 現有群益授權、實盤前檢查、COM session 及對帳不得更動 | **不採用下單整合**：只參考精確版本與不可改寫記錄。新模組沒有 broker tool、COM 或正式執行器 import |
| trader-pelosi（兩版） | House Clerk ZIP／PTR PDF，披露延遲與金額區間 | 美國政治人物申報與台股候選無直接同等資料 | **不採用策略訊號**；保留公告時間與事件來源的研究原則 |
| trader-copycat（投資版） | SEC 13F／Form 4、CUSIP 映射、目標權重；季末持股有申報延遲 | 不等於台股即時法人買賣；不能將晚公布持股放回季末 | **不採用本輪策略**；若未來研究法人風格，另建披露時點資料集 |
| trader-screener（投資版） | 基本面 JSON／CSV 的宣告式規則、缺值 fail／skip、加權排名 | 目前基本面歷史來源未完整，已有日線候選實驗 | **延後改造**：缺值保留與 fail 原則納入 Skill，未將沒有時點的基本面加入排名 |
| trader-rebalance（投資版） | 權重漂移帶、先賣後買、假設可使用預期現金 | 目前有未結委託與交割，不能直接把賣單當已收到現金 | **延後採用**：可參考漂移原理，但須使用實際成交／交割資金模型，未啟用自動再平衡 |
| trader-tax（投資版） | FIFO/HIFO／指定批次、持有期與美國 wash-sale、tax-loss harvest | 美國稅制不能套入台股交易稅 | **不採用稅務策略**；只參考成本批次完整性，本次使用明列台股賣方交易稅假設 |

LLM 方面，上游 Skills 主要是自然語言步驟加 Python CLI，不是已訓練的交易模型，也沒有 LLM 收益評估或 API 成本框架。現有 STOCKLLM 已有 Responses／Agent 工具與驗證，故新 Skill 使用相同的凍結證據與結構化輸出概念，不以長篇提示詞取代程式風控。

## 原測試通過仍存在的語義限制

`upstream-semantic-probes.json` 是本輪另外實跑的離線反例，未修改 upstream：

1. `trader-news/news.py:select` 接受 `now + 10 minutes` 以內的新聞；決策時間後 5 分鐘的資料會被納入。歷史回放須另做嚴格 cutoff。
2. `trader-analyst/facts.py:periods` 沒有 as-of 參數，同一期間會選到 2027 年才申報的重編值。這對當前報告是設計選擇，對過去決策不成立。
3. `trader-timemachine/timemachine.py:simulate` 抽到 open=80、entry=100、stop=98 的棒，仍將停損記為 -1R；較差開盤價及費稅沒有反映。新 `exit_price` 對該情況回傳 80。

因此 490 項原測試只代表原作者的程式契約與 fixture 可運作，不代表台股資料、真實成交、機率或盈利已驗證。

## Phase 3：台股適配

`market_rules.json` 集中管理市場、幣別、時區、整股／零股單位、交易窗口、交割與跳動單位；策略不再沿用美東 DST、78 棒或美元美分取整。台股普通股的漲跌停例外不能由程式一律推定為 10%；Paper 前向介面要求來源已核實的實際上下限。

規則以生效日選用。2026-11-25 適用原零股 09:10 首次撮合及千元以上 5 元跳動；[證交所 2026-11-18 公告](https://www.twse.com.tw/staticFiles/news/news/tsecnews/8a8216d6a19afd9c01a1edd070c8053b.pdf)指出 2026-12-07 零股交易時段調整、2027-07-05 千元以上改為 1 元跳動。交易單位及一般價格限制另核對[證交所制度](https://www.twse.com.tw/zh/products/system/trading.html)與[櫃買中心制度](https://www.tpex.org.tw/zh-tw/mainboard/trading/rules/system.html)。未來制度仍須在前向期間再核對，不能因保存公告就視為永久不變。

研究賣出稅率採 0.3%，不自動套用當沖減半。[財政部說明](https://www.etax.nat.gov.tw/etwmain/tax-info/understanding/tax-knowledge/rwG2M1N)列明股票一般賣出稅與符合條件的當沖差異。最低手續費及滑價為明列研究假設，尚未核實帳戶合約。

群益快照沿用 CAPITAL_SKCOM、零股來源、非試撮、雙邊價與時間證據。舊快照沒有 exchange、商品分類、實際漲跌停與以股為單位的雙邊深度，前向介面會拒絕成交，不補猜。沒有新開券商 session，也未把 Yahoo 非官方行情作為即時成交價。

## Phase 4：Time Machine 方法

日線 moving-block 保留連續 3 個交易日 OHLC 相對前收的結構，不跨缺日、不循環接尾、不使用決策日之後資料。候選池最多 252 個過去交易日；至少 20 個可用區塊，資料不足保持缺漏，不偷偷改回無條件模型。過濾實際成交金額與零成交量。

條件版本使用「固定研究母體等權價格序列的 20 日方向及波動」作市場狀態代理，並以個股過去 20 日波動的 0.5–2 倍篩選。樣本區塊的條件來自區塊開始前，不能利用區塊結果決定其狀態。固定門檻沒有在測試結果上搜尋；它們也沒有被證明最適。

模擬及回放含最低佣金、稅、滑價、台股跳動單位與跳空損失；同棒雙觸價採停損優先。每日時間步長無法重現盤中順序、停牌、漲跌停排隊與零股撮合。模擬沒有完整商品別未來漲跌停參考與公司行動模型，仍不得當作真實成交勝率。

固定五個機率區間、Beta(1,1) 前序校準，只使用完整觀察期限已在決策前成熟的標籤。報告同時保留 raw、prequential、校準可用樣本上的 matched raw，以及三模型共同覆蓋樣本，避免在不同分母上宣稱校準或模型改善。計算 Brier、log loss、ECE 與可靠度區間統計；此校準標籤是候選情境，與實際帳戶成交標籤不同。

逐季 walk-forward 使用逐日擴展的過去資訊，沒有用未來標籤訓練；但既有資料的發布時點、公司行動與歷史母體仍不合格。因此這是按時間切分的回顧診斷，尚不是可認證的獨立 Out-of-Sample 證據。

## Phase 5：實驗與結果

正式結果、各版本全部指標、成本敏感度、分季結果、校準與 CLI 成本詳見同目錄 `experiment_results.md` 與機器可讀彙總。所有成本格都保留，不以最好的格子推廣策略。

共同協議：初始資金 NT$100,000、每筆 NT$10,000 上限、每日買入 NT$20,000 上限、最多 6 檔、每筆名目停損風險不超過初始資金 1%、零股不超過 999 股、以前一日成交量 1% 限制股數、同一日線與次日開盤成交模型、進場當日不退出、最多持有後續 5 個交易日。賣出淨款保留兩個後續交易日；不靠未交割賣出款擴張買入。

資產淨值由現金、未交割淨款與持倉扣假設清算成本的價值組成。Win Rate／Expectancy／Profit Factor 只取已平倉交易；Net Return／Drawdown／Sharpe 使用整個帳戶。Benchmark 為同一固定研究母體等權買入持有、含同樣費用及滑價，不是加權指數。尚未成熟、缺價與持倉末期狀態另存，不補成勝負。原資料 20,435 筆、22 檔，測試切片 2026-01-02 至 2026-11-10；615 筆凍結候選中 597 筆完整期限成熟、18 筆未成熟。

## Phase 6：版本、前向、CI 與回退

所有回放寫入全新研究目錄；舊結果不覆寫。來源、協議、資料與 Skill 均保存雜湊及原檔。Paper 前向版本、控制、決策、成交與原碼備份用 SQLite trigger 禁止 UPDATE/DELETE；時間必須向前、重複輸入不能再次成交、部分成交保留剩餘數量及成本。

原碼改變須登錄新版本與獨立帳戶。回退先停用新版本，再恢復已保存舊原碼並啟用舊版本；舊帳戶延續，不能重置過去損益。此模組是獨立研究環境，未接入正式 strategy_experiment 的 GUI 或資料庫，也沒有更動既有可刪除策略的介面。

新增 GitHub workflow 在 Windows／Linux 執行 Unit、Integration、Future Data Leakage、Portfolio Accounting、Paper Trading Safety 及既有選股／標籤測試。Actions 固定 commit，無憑證、行情下載、LLM 呼叫或部署步驟。本次尚未推送，遠端 CI／Linux 結果待執行；本機 Windows 結果與首次失敗、修正紀錄均保留。

## 程式修改紀錄

| 檔案 | 修改目的 |
| --- | --- |
| `research/trading_skills/audit.py` | 完整來源、依賴、設定、授權與原測試證據 |
| `market.py`、`market_rules.json` | 日期化台股規則、Decimal 成本、唯讀群益快照驗證 |
| `entry.py` | 完整收線 ORB，缺棒與未完成棒拒絕確認 |
| `simulation.py` | 因果抽樣池、IID／區塊、費後模擬、跳空出場與前序校準 |
| `experiment.py`、`protocol.json` | Baseline 凍結、同條件帳戶比較、OOS 時間切片與全部指標 |
| `evidence.py` | 新聞／基本面的發布、取得及重編時點 |
| `forward.py` | Paper 專用 append-only 帳戶、版本、部分退出、交割保留與回退控制 |
| `review.py`、`skills/tw-trading-decision/SKILL.md` | 配對 CLI 研究、結構化輸出、證據檢查與使用量 |
| `__main__.py`、`README.md`、`THIRD_PARTY_NOTICES.txt` | 啟動、重現、限制、授權與操作說明 |
| `tests/test_trading_skills.py` | 五類新測試及分 K／新聞／校準安全 |
| `tests/test_strategy_experiment.py` | 修正同一時鐘 tick 建立版本時以清單位置辨識帳戶的不穩定測試；改用實際 strategy_id，正式程式不變 |
| `.github/workflows/trading-skills-research.yml` | 離線跨平台檢查，沒有實盤部署 |

修正過程另外處理 Windows SQLite context 不自動關閉造成的檔案鎖、繼承到不完整 GIT_CONFIG_* 時的來源雜湊查核失敗，以及日文 Windows CP932 對繁體中文 CLI 輸出的編碼錯誤。早期失敗輸出留在研究目錄，沒有覆寫為成功紀錄。

## 尚未解決及下一步

1. 取得可驗證的還原價、公司行動、歷史母體、公告原始發布／取得時間；目前原始價格與當前母體不具備認證回測資格。
2. 從現有唯讀行情擷取流程增加獨立的商品資料、實際漲跌停與零股雙邊深度證據；沒有這些欄位時前向模組按預期拒絕成交。不能改用 Yahoo 填補。
3. 在同一逐筆凍結行情、帳戶與模型成本下重播正式 LLM Baseline 及研究新版本；保留工具失敗、模型費用、未成交與延遲。既有歷史決策含原帳戶上下文，不能直接換資金後當成同條件反事實策略回測。
4. 為 MOPS／IFRS 科目、新聞全文與完整分 K 建立測試 fixture；目前只完成通用時點介面及 ORB 單元測試，尚未完成真實台股公告擷取與真實分 K 前向驗證。
5. Paper 價格／深度模型仍未包含撮合佇列、部分成交的所有市場機制、交割假日例外與真實帳戶費率。日線 Monte Carlo 的次日開盤標籤也不能直接校準即時買賣價成交。
6. 持續保存新交易日的影子／Paper 結果，等完整持有視窗成熟，按預先固定指標及成本敏感度評估。暫不調參追逐本輪最佳版本，也不升級實盤。
7. 遠端 CI 尚未執行；本機通過不等於已發布或已常駐部署。正式排程、實盤授權及歷史交易未改寫。
