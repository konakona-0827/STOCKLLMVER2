# 本輪程式修改紀錄

本輪延續既有 `research/trading_skills`，新增正式 LLM 比較及群益唯讀 Paper 執行層。正式策略、驗證器、交易前審查與實盤授權流程未改寫。

| 檔案 | 修改目的 |
|---|---|
| `research/llm_paper/common.py`、`protocol.json` | 固定兩組資金、模型、成本、成交、新鮮度與呼叫上限；版本來源凍結與驗證 |
| `data.py` | 依原請求時點建立 85 截點 cohort，驗證行情／日線／接收時點，移除歷史真實帳戶上下文 |
| `engine.py` | 共用 Paper 帳本、部分成交、每委託累計費稅、限價、FIFO、T+2、同日零股禁賣、ROD、淨清算估值與 checkpoint 累積風險 |
| `llm.py` | 沿用正式 builder／validator／reviewer；每組獨立帳戶，封存所有呼叫，禁止搜尋／API fallback，保留缺失用量；修正空錯誤訊息 |
| `model_worker.py` | 可被終止的模型子程序；外層 150 秒 deadline 與自有程序樹限時清理，避免 CLI pipe 回收卡住 |
| `replay.py` | 閉環配對重播；有限並行預取，完整 request hash 相同才採用；作廢成本仍記錄 |
| `capital_feed.py` | 只建立群益中心／回報／行情 COM；market=5 零股與五檔回呼第一檔、實際漲跌停、獨立深度接收時間 |
| `data_sources.py` | 官方商品／基金主檔、年度日曆及完成日 K；raw receipt 與 hash；修正日曆 date 參數、限時來源下載 |
| `live.py` | 群益新鮮行情及官方日線建立共同排名與策略證據；缺資料不開新倉 |
| `journal.py` | SQLite append-only trigger、hash-chain 與恢復 checkpoint |
| `runtime.py` | 常駐雙組 Paper、盤外等待、30 分鐘 slot、行情與 LLM 分離、過期拒絕、版本／OS lease、控制檔與唯讀行情連線 |
| `release.py`、`__main__.py`、`README.md` | 隔離 frozen export、CLI、啟停／回退操作；不複製正式資料庫、.env 或 execution 套件 |
| `skills/tw-paper-decision/SKILL.md` | 原研究 Skill 的 Paper 決策適配；沿用正式 BUY/HOLD/SELL/WAIT schema，不能啟用實盤 |
| `tests/test_llm_paper.py` | 新增 25 項正式配對、未來洩漏、會計、深度與 Paper 安全測試 |
| `.github/workflows/trading-skills-research.yml` | 加入新模組／Skill 觸發路徑與 LLM Paper 測試 gate |
| `reports/llm_paper_20261125/*` | 實驗設定、356 項結果、完整重播彙總、唯讀獨立帳務核對及部署證據 |

本輪迭代時先停止舊 forward worker，再輸出新 release、建立新空帳戶；所有舊版本、錯誤、控制停止事件及重播結果均保留。部署 v3 的正式策略與決策驗證邏輯已和歷史 replay release 核對一致；worker 逾時與 checkpoint 改善屬執行可靠性修正。

本機完整檢查：`python -X utf8 -m unittest discover -s tests -v`，356 tests，OK。真實群益 probe 為唯讀，模型 smoke 使用現有 ChatGPT CLI，沒有發出券商委託。

唯讀重播核對（輸出使用新檔名，禁止覆寫既有證據）：

```powershell
python -X utf8 reports/llm_paper_20261125/audit_replay.py --study research-runs/llm-paper-20261125 --output research-runs/llm-paper-20261125/audit-check-new.json
```

該核對包含 Paper 盤外啟動狀態；進入交易日後狀態會改變，不應拿新的運行狀態改寫本報告保存的部署快照。
