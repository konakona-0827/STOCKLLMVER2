# 台股多時間尺度實驗立項與離線驗證

日期：2026-10-30（Asia/Tokyo）。

分支 `codex/tw-multi-horizon-experiment` 從 `develop` 的 `5ec30f1` 建立，使用既有 STOCKLLM checkout。規劃見 [A/B/C 改造計畫](../docs/experiments/TW_MULTI_HORIZON_PLAN.md)。

## 本輪結果

使用者指定的 `20261030GPTCONVERSATION.md` 已依原始位元組封存，SHA256 比對一致。新增 `research.multi_horizon` 標準函式庫入口、分線協議、合成範例與操作文件。

目前完成 M0 與價格路徑標籤基礎。A/B/C 尚未接入真實歷史研究資料、校準模型、組合回測或前向帳戶。既有正式策略選擇、Scheduler、帳本及執行流程沒有程式變更。

## 驗證證據

- Python 3.12，新增 21 項研究測試通過。
- 全套 `tests/test_*.py` 共 256 項通過，另 3 項 advice interface 通過，合計 259 項。
- 全套回歸使用暫存 `TRADING_STATE_ROOT`、空 `.env`、複製的 config／strategies 及測試資料庫，並移除測試子程序的模型／券商憑證環境變數。
- CLI 隔離測試刻意指定不存在的正式 ROOT，仍可完成研究；檢查未匯入 `config`、`capital`、`openai`、`strategy_experiment` 或 `paper_portfolio`。
- `python -m pip check`、`git diff --check`、新增 Python 語法與文件相對連結檢查通過。
- 合成範例 run：`20261030T181153892580-9859e0a6`，12 筆虛構日線產生 204 筆標籤，分別保存 A/B/C 摘要。輸入快照及實作 SHA256 核對一致。
- 合成結果位於本機 Git 忽略的 `research-runs/`，並非真實市場績效證據。驗證沒有呼叫付費模型、券商登入或正式委託。

未成熟視窗即使已提早觸價，也不納入摘要平均或觸價比例，避免資料尾端只保留已退出樣本。完整標籤仍保存供後續研究。

## 下一階段

依 M1 建立多年交易日曆、股票歷史母體、價格與公司行動處理，再固定時序切分、訓練量化基準及校準。成本與 T+2 的完整帳戶層模擬列入 M2；此輪沒有修改舊模擬帳本或重算既有收益。
