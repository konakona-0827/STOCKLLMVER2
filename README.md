# Support Desk 0.4：尋標 → 研究 → 反證

分支：`codex/support-desk-discovery`。2026-10-28 建立、10-29 完成首輪驗證，沿用 `codex/support-desk-phase1` 的固定支撐與獨立複核，將入口改為真正可使用工具的尋標子代理。原分支及交易系統 develop 維持獨立。

目前預設共三個 Agent：

1. **尋標 Agent**：自行查動態市場名錄、選擇產業／分頁／排序，再指定股票讀取日線，提出附證據的線索。
2. **研究員**：依提名股票的同一份日線快照研究支撐、反彈、回測與失效條件。
3. **反證員**：獨立檢查同一股票，可以否決；不先看到研究員的答案。

Python Head 負責分派、額度、來源核對與數值檢查，不預設股票池、不下單。尋標角色的數量與任務由 [config/discovery.json](config/discovery.json) 載入，未來新增不同方向的尋標者不需改 runtime。

```mermaid
flowchart LR
    H[Head：任務及額度] --> F[尋標 Agent]
    F --> T[動態市場查詢／日線工具]
    T --> F
    F --> L[附來源與固定編號的線索]
    L --> R[研究員]
    L --> V[反證員]
    R --> G[Python 證據及硬性檢查]
    V --> G
    G --> O[觀察報告與固定支撐追蹤]
```

## 先驗證架構

Python 3.11 以上，執行期沒有第三方套件相依。以下不讀 key、不呼叫網路或模型：

```powershell
python -m unittest discover -s tests -v
python -m support_scout discovery-demo --out output/demo
python -m support_scout discovery-replay output/demo/產生的RUN_ID
```

`discovery-demo` 是明示的腳本與合成資料，用來檢驗工具迴圈、提名交接與兩方複核，不能當作模型已找到真實機會的證明。

## 使用動態尋標

本機 `local_settings.json` 沿用已授權的 OpenAI key／模型來源；值不進 Git 或報告。設定不完整會停止，不會退回舊 19 檔池。

```powershell
.\Doctor.cmd
.\Discover.cmd
# 初期可再縮小本輪探索／提名數量
.\Discover.cmd --max-leads 2 --max-history-symbols 3 --max-packets 2 --max-calls 4
# Observe.cmd 在此分支也改走 discover
.\OpenReport.cmd
```

也可 `python -m support_scout discover --no-review` 只收集未複核線索。手動觀察仍保留 `python -m support_scout observe --symbols 2330,2454 --no-agents`，必須明確指定股票；不再自動讀固定名單。

預設尋標最多 8 個模型回合、12 次工具呼叫、深入 4 檔日線、提名 3 檔；後續研究／反證另保留配對額度，預設最多 5 檔／10 次。所有模型回合均列入稽核，工具有各自上限，沒有偷偷自動重試。這些是每輪限額，尚非每日美元硬上限。

市場來源為 TWSE 官方動態上市公司名錄與全市場收盤行情，不含上櫃、日本或 ETF。市場名錄可以有上千檔，實際探索仍受額度限制；報告會列出看過、讀過日線、提名、交付複核的數量，不能將空名單解讀成整個台股市場沒有機會。

報告在 `output/discovery/<RUN_ID>/`，包含市場快照、工具選擇與回應、日線、角色任務、模型用量、提名與研究結果。`discovery-replay` 只重播保存資料，不重新連線或問模型。原始價／公司行動與事件缺口仍固定保留 OBSERVATION ONLY，不能因三個 Agent 都同意就解鎖。

[三 Agent 架構與驗收](docs/discovery-architecture.md) · [後續路線](docs/roadmap.md)

已通過 154 項離線測試，並完成一輪真實尋標 → 日線工具 → 雙方研究及完全離線重播。這一階段確認架構能運作，下一步才逐步驗收尋標品質與擴大探索。

## 0.3 舊分支操作紀錄

以下保留 `codex/support-desk-phase1` 的固定池操作與歷史驗收；本分支的新入口與範圍以以上內容為準。

### Support Desk：台股支撐觀察

將 Advantest 的「先反彈、回測原支撐、後續確認且仍接近支撐」概念用於台股。已有官方日線擷取、獨立雙 Agent、跨日固定支撐追蹤與本地 HTML 報告，可用於收盤資料觀察。

分支：`codex/support-desk-phase1`。這是獨立專案，存放於既有 STOCKLLMVER2 遠端的獨立分支，與 develop 沒有共同歷史；請直接 clone 此分支，勿把它當作可直接 merge develop 的變更。Python package 保留 `support_scout` 名稱。

```mermaid
flowchart LR
    A[TWSE官方日線與實際交易日] --> B[資料完整性與新鮮度]
    B --> C[固定支撐掃描]
    C --> H[Head 依優先度與額度分派]
    H --> R[Researcher 支撐研究]
    H --> V[Reviewer 獨立反證]
    R --> G[Python 固定裁決]
    V --> G
    G --> L[跨日SQLite追蹤]
    L --> O[HTML報告與可重播快照]
```

## 現在怎麼用

Python 3.11 以上，無第三方執行期相依套件。在本資料夾：

```powershell
.\Doctor.cmd
.\Observe.cmd
.\OpenReport.cmd
```

本機已依使用者授權，在 git 忽略的 `local_settings.json` 指向 STOCKLLM 的 OpenAI 設定，僅讀取 API key 與模型。金鑰不複製到報告、不提交；不匯入券商模組。新 checkout 請將 `local_settings.example.json` 複製為 `local_settings.json`，指定自己的設定檔與模型。

預設觀察固定 19 檔上市普通股，每輪最多選 5 檔做雙 Agent 研究、最多 10 次模型呼叫。先處理完整且接近支撐的候選，已遠離支撐的觀察排後面。沒有合格標的不湊數。執行模型會產生 API 費用。

```powershell
python -m support_scout observe --no-agents
python -m support_scout observe --agents --symbols 2330,2454,2317 --max-packets 2 --max-calls 4
python -m unittest discover -s tests -v
```

報告存於 `output/observations/<run_id>/report.html`；`latest.json` 指向最新完成的報告，`latest_attempt.json` 記錄最後嘗試。失敗有自己的紀錄，不把舊報告冒充新結果。最新指標有程序鎖及時間比較，舊資料重跑不能覆蓋新報告。

## 觀察能力與界線

- 官方 TWSE 月日線取 8 個月；市場日曆來自 FMTQIK 實際交易日，並以年度假日及台北 16:00 截止獨立檢查應有最新交易日。
- 每份來源保存原始 JSON、取得時間與 SHA256。缺月、漏日、錯代號或過期尾端均明示，失敗股票不補前值。
- Researcher 與 Reviewer 用相同快照、不同上下文；反證員可否決，身分與時間由程式分配。
- SQLite 保存原先支撐下緣／上緣；一旦失效不能同型態復活。資料修訂另標記，同日去重，倒退資料不覆寫目前狀態。
- 報告先在 SQLite transaction 中完整發布，再提交帳本與完成標記；失敗回滾，最新指標只在完成後更新。若程序在發布與提交之間中止，沒有 `committed.json` 的目錄不應視為成功。
- HTML 顯示暫定支撐、最新收盤、距離、失效條件、來源、兩方評語與跨日變化。

**目前是 OBSERVATION ONLY。** 公開資料採原始價格，尚未核實完整公司行動、公司公告／未來事件及官方鎖價狀態。這些缺口固定阻擋 `RESEARCH`，即使兩個模型同意也不會解除。平價 K 只是鎖價疑慮的保守代理。沒有下單、帳戶、持倉執行或 5% 帳戶停工功能。

這次真實執行已覆蓋 19 檔、每檔 162 根日線至 2026-10-27，並完成 5 檔／10 次模型研究。當時沒有完整規則確認且仍接近支撐的技術候選，輸出空候選名單是正常結果，不是系統失敗。

## 支撐規則與重播

至少 60 根日線；低點較前 20 日高點回撤 8%，左右各兩日低點較高，才在右側資料可得時凍結支撐。支撐區為 `低點 ± max(0.35 × ATR14, 低點 × 0.8%)`。另日反彈 1.5 ATR，之後另日回測未破、後續收盤突破回測日高點，才標規則確認。距上緣超過 2.5 ATR 不列技術候選；收盤跌破固定下緣即失效。這些是待驗證假設，分數不是勝率。

每輪保存 `input.json`、`rules.json`、`manifest.json`、`report.json`、任務與評語。manifest 保留實際截止、規則、來源程式 hash 與輸入 digest。

```powershell
python -m support_scout scan output/observations/RUN_ID/input.json --rules output/observations/RUN_ID/rules.json --out output/replay
```

[10/28–10/29 操作方式與驗收](docs/observation-runbook.md) · [獨立審查](docs/model-review.md) · [架構](docs/architecture.md) · [資料合約](docs/data-contract.md) · [路線](docs/roadmap.md)
