# 開源交易 Skill 的台股 Paper 實驗

研究版 `tw-trading-skills-v1`。只讀取資料與既有群益快照，所有新帳戶位於 `research-runs/`。不載入正式設定、正式帳本、COM、執行器或 API 金鑰；一般研究及測試只需 Python 3.12 標準函式庫。可選的 CLI 意見比較使用既有 `codex_cli.py` 與其已安裝的 OpenAI 型別套件，沿用 ChatGPT 登入，不切換付費 API。

## 重現研究

在 STOCKLLM 執行。每次指定全新輸出目錄；已有目錄會拒絕覆寫。

```powershell
git clone https://github.com/Jakeschincariol/day-trader-skill.git ../.research-upstream/day-trader-skill
git clone https://github.com/Jakeschincariol/trader-skill.git ../.research-upstream/trader-skill
git -C ../.research-upstream/day-trader-skill checkout --detach 3d0f4ef6986f4984b7c6bd46f7ff4f87744c3abd
git -C ../.research-upstream/trader-skill checkout --detach 5189e79b9a480755a3733ac9713f5353981dbe18
python -m research.trading_skills audit --repo ../.research-upstream/day-trader-skill --output research-runs/new-study/day-upstream
python -m research.trading_skills audit --repo ../.research-upstream/trader-skill --output research-runs/new-study/invest-upstream
python -m research.trading_skills replay --dataset research-runs/selection_lab_20261111/dataset --output research-runs/new-study/base
python -m unittest discover -s tests -p test_trading_skills.py -v
```

`audit` 讀取每個 tracked 檔，保存 SHA256、AST、全部 Skill／原始碼／設定／測試文字、授權與原測試輸出。兩套 upstream 都沒有 Python 套件安裝清單，原測試只用標準函式庫；PTR PDF 轉文字另需系統 `pdftotext`，Alpaca MCP 是外部選用依賴，本實驗不安裝。

`replay` 固定既有 selection_lab 動能排名前 3 名，不在新模組排除後遞補。五個版本共用資金、費用、次交易日開盤模型、成交量限制、持有期限與 T+2 賣出款保留；比較形態、IID、區塊及條件區塊抽樣。實驗保存 Baseline 原檔、所有實驗原碼、Skill、協議及資料雜湊。正式 LLM 策略保持原樣；日線研究 Baseline 不能冒稱正式 LLM 的完整回測。

本輪舊資料為固定當前母體、未核實還原價與發布時點。即使計算了分季 walk-forward 與前序校準，也只屬回顧診斷。市場狀態是研究母體等權指數的趨勢與波動分類，並非加權指數；校準使用相同凍結候選的費後情境標籤，不代表實際成交勝率。查看 `summary.json` 的警告、覆蓋率、未成熟數及逐筆缺資料狀態。

## 可選 LLM 比較

以下命令消耗 Codex 方案／credits 額度；不執行時不呼叫模型。CLI 只讀取凍結候選及無未來標籤的模型資訊；工具陣列為空，意見不接入下單。

```powershell
python -m research.trading_skills review --run research-runs/new-study/base --transport codex_cli.py --output research-runs/new-study/cli
```

每份意見必須包含正確 packet_id、逐檔 evidence_id、RESEARCH/WATCH/REJECT、假說、反證及缺漏。失敗與使用量原樣保留，不重試、不將失敗當 WATCH。這只驗證傳輸、提示與研究品質，沒有足夠前向標籤時不能推論收益增益。

## Paper 前向介面與回退

```powershell
python -m research.trading_skills forward-init --store research-runs/my-forward/paper.sqlite3
python -m research.trading_skills forward --store research-runs/my-forward/paper.sqlite3 --version <回傳版本> --snapshot <唯讀快照.json>
python -m research.trading_skills control --store research-runs/my-forward/paper.sqlite3 --version <回傳版本> --state inactive
```

上游行情擷取器可在每次凍結快照後呼叫 `forward`；本次沒有改正式排程或啟動常駐程式。先以影子方式檢查缺漏，再累積完整期限的結果。本機登錄本身不代表資料已齊或已部署成功。

快照必填 `mode: PAPER_ONLY`、含時區的 `at`、排序且不重複的 `sessions`、`historical_membership_verified: true`、`bars`、`quotes`。日曆須含持有期限與交割所需的未來交易日；未來日曆不是未來價格。所有日線須具備 symbol、date、OHLC、正整數股數 volume、實際 turnover_twd、available_at、retrieved_at 與 64 位來源 SHA256；發布與取得時間均不得晚於決策，並檢查完整歷史與交易日連續性。

群益 quote 延用 `multi_market_snapshot.json` 的 source=CAPITAL_SKCOM、quote_basis=capital_intraday_odd_lot、retrieval_mode=BROKER、quote_status=LIVE、is_trial=false、quote_timestamp、received_at、bid、ask、volume_unit=shares、evidence_id；另需由已驗證的商品資料與深度快照補上 exchange、instrument_type=COMMON_STOCK、limit_down、limit_up、bid_size_shares、ask_size_shares。既有快照缺這些欄位時會拒絕成交，不能推測深度、把整股量當零股量或將 nRef 當完整漲跌停資訊。

Paper 成交採對手價加不利滑價、保守跳動單位及可見深度上限；這仍是模型，沒有證明排隊可成交。日線 Monte Carlo 模組在前向介面只作費後期望值研究濾網；它的次日開盤假設與即時對手價成交不同，必須另作前向校準，不沿用日線校準數字當即時勝率。`entry.opening_range_signal` 已提供完整收線 ORB 偵測，但本輪日線實驗與前向預設沒有開啟它；不得用日線替代分 K。

版本、控制、決策、成交與原碼備份均不可 UPDATE/DELETE。同版本時間須向前，重複決策不能重新成交，賣出款按供應日曆 T+2 保留，部分退出按比例保留成本。原碼改變須登錄新版本及獨立帳戶。回退先停用新版本，再使用保存的舊原碼重新啟用舊版本；其現金與歷史繼續保留，不能把新程式套在舊版本上。不要將 `data/execution` 的正式資料庫作為 store。

## CI 與後續門檻

GitHub workflow 在 Windows／Linux 執行五類新測試及既有選股／標籤測試，不下載行情、不呼叫 LLM、不觸發部署。尚未推送，因此遠端 CI 狀態仍待執行。升級前要補齊正式 LLM 的可重現決策輸入、群益零股深度、正式商品及日曆來源、還原價、公司公告發布／取得時間、真實費率與較長成熟前向結果。沒有這些證據時保持 Paper／研究，不提升為實盤。
