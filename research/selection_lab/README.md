# 台股選股實驗與 CLI 試運轉

2026-11-11 建立，分支 `codex/tw-selection-lab`。此版本把 FinMind 免費資料轉成共用格式，實際接入 Qlib 0.9.7、Alpha158、LightGBM 及 backtesting.py 0.6.6，並透過既有 Codex CLI transport 完成四次研究呼叫。

目前適合前向研究試運轉。這次原始價格重算中，Qlib 排名的正收益案例比例略高，但三種方法的平均費後情境報酬都為負。公司行動、歷史母體與歷史公告時間尚未核實，尚無正式策略替換依據。

## 實跑結果

免費資料使用目前固定的 22 檔上市／上櫃普通股，2023-01-01 至 2026-11-10 共 20,435 筆有效日線。保留實際成交金額、取得時間及原始來源 SHA256。2317、1101、5347 各有一筆無效價格，獨立記錄，不補價或壓縮交易日。當前母體不代表歷史全市場。

Qlib 實際計算 158 個特徵，LightGBM 訓練 14,107 筆樣本；所有訓練標籤的完整視窗須在 2025-12-31 前結束。2026 年有 4,510 筆候選觀測，每日取前三名。615 筆選取中，597 筆可評分、18 筆未成熟；未成熟候選保持原名次，不遞補。

| 方法 | 正收益案例／可評分案例 | 正收益案例比例 | 平均逐筆費後情境報酬 |
| --- | ---: | ---: | ---: |
| 20 日動能基準 | 284／597 | 47.57% | -1.87% |
| 固定複合排序 | 279／597 | 46.73% | -1.61% |
| Qlib Alpha158＋LightGBM 診斷 | 289／597 | 48.41% | -1.16% |

以上不是原正式策略的回測，也不是組合績效。研究設定為每筆 NT$10,000、股價 +2%／-2% 障礙、買入後五個交易日觀察、次交易日開盤情境進場，進場當日不退出。最低手續費每邊 NT$20、未折扣費率 0.1425%、賣方稅 0.3%、每邊 10 bps 滑價都是凍結研究假設，未讀取正式帳戶費率。日線雙觸價採停損優先、跳空跌破停損採較差開盤價。逐筆機會可能重疊，不能據此計算帳戶報酬或最大回撤。

backtesting.py 完成 22 檔輸入與次日開盤成交的機械測試。其測試沒有模擬這份研究的完整稅費、最低費用、停利停損、零股成交或交割規則，因此沒有把兩套引擎績效視為等價。

CLI 實測比較「動能／複合排名 × 一般提示／專案 SKILL」，四次都通過 schema、packet_id、候選覆蓋與 evidence_id 檢查。約 85.83 秒、56,184 輸入 tokens、2,803 輸出 tokens；其中 11,008 輸入 tokens 為快取。這是研究輸出格式與傳輸測試，不是研究員／反證員的完整獨立決策流程，也尚無未來結果可評分。兩份提示在這個截點給出的分類相同，不能證明 SKILL 增益。

## 成本與替代範圍

價格查核日為 2026-11-11。以下為個人研究用途，另加既有電腦、網路與 Codex 訂閱；未包含券商、實際交易費用及可能購買的額外 Codex credits。

| 路線 | 費用 | 適用情況 |
| --- | --- | --- |
| FinMind Free＋本機 Python | 資料 NT$0 | 原始日線、成交金額、資料與排名試驗 |
| 上述＋Codex CLI | 本輪 API 費 US$0；消耗 Codex 方案／credits 額度 | 取代 API 型的研究文字推論 |
| FinMind Backer | NT$699／月，或 NT$5,499／年 | 需要還原價、交易限制等會員資料時 |
| FinMind Sponsor | NT$999／月 | 確實需要分 K／即時等額外資料時再評估 |
| FinLab VIP | NT$888／月；最低 12 個月，原價最低 NT$10,656 | 需要 FinLab 最新台股資料與研究工具；優惠另依適用條件 |

[FinMind 方案](https://finmind.github.io/Pricing/)將還原價列入 Backer；免費原始價格不能假裝已還原。[FinLab 方案](https://finlab.finance/pricing)的免費台股資料至 2023 年底，新帳號有七天 VIP 試用；本次沒有建立帳號、開通試用或購買方案。

若以這四次 CLI 的相同 token 數估 API 預算，使用 [GPT-6 Sol 標準價格](https://developers.openai.com/api/docs/models/gpt-6-sol)且全部不快取，約 US$0.1404／組。每日一組、22 個交易日約 US$3.09／月；每日七組約 US$21.62／月。保留實測快取比例時本組約 US$0.1206。這是等值 token 預算，CLI 的系統與 schema 開銷不會與直接 API 工作量完全相同，也不是本輪帳單。

[Codex 方案與額度](https://learn.chatgpt.com/docs/pricing)依模型、任務及方案而異，不能用這些 tokens 推算剩餘方案百分比。CLI 是雲端推論，不是本機離線模型。已訂閱且額度足夠時可避免額外 API 計費；耗盡時停止，不切換 API。本輪沒有查得額外 credits 的扣款狀態。

Python CLI 可自行處理下載、特徵、排序、標籤與報告，替代 FinLab 對應的部分研究工作；不能憑 CLI 取得付費資料，兩套回測引擎也不會自動等價。Qlib 本身為 MIT，backtesting.py 為 AGPL-3.0，FinLab Python 套件標示 GPL-3.0-or-later；資料服務與程式授權分開處理。這次完整可選研究環境約 0.96 GiB，基本下載與規則比較只需 Python 標準函式庫。

## 啟動

在 STOCKLLM 目錄執行，產出一律寫入忽略的 `research-runs/`，每輪建立新目錄。程式不匯入正式設定、券商、帳本或下單模組。

純 Python 免費資料版本：

```powershell
.\RunSelectionLab.ps1
```

需要 Qlib 診斷或 CLI 研究時，先安裝獨立環境：

```powershell
python -m venv research-runs/.venv-selection
research-runs/.venv-selection/Scripts/python.exe -m pip install -r research/selection_lab/requirements-optional.txt
```

下載最新已完成日期以前資料、比較排名、產生含 Qlib 排名的凍結 packet，並執行四次 CLI 研究：

```powershell
.\RunSelectionLab.ps1 `
  -PythonPath .\research-runs\.venv-selection\Scripts\python.exe `
  -WithQlib -Review `
  -TransportFile ..\STOCKLLM-plannedlimit-codex\codex_cli.py
```

`-Review` 明確消耗 Codex 額度；省略時不呼叫模型。須已用 ChatGPT 登入原生 Codex CLI。transport 路徑須指向目前既有、已核對的 `codex_cli.py`，其雜湊會保存。沒有 API fallback。候選資料沒有金鑰或帳戶欄位。腳本的資料截止預設為台灣昨日，再由實際日曆選最新交易日；同日盤後資料留給後續核實發布時間的 adapter。

重用已保存資料，可避免重新下載：

```powershell
.\RunSelectionLab.ps1 -Dataset research-runs/selection_lab_20261111/dataset
python -m research.selection_lab --help
```

`qlib_raw_features` 匯出不含虛構的 factor=1。此版本用原始 OHLCV 做 Qlib 特徵診斷，明確標示未核實公司行動；補齊還原資料後另建版本。模型分數檔須與資料、協議雜湊及訓練截止相符才能併入 packet。

## 下週試運轉

預定觀察期間為 2026-11-16 至 2026-11-20，採手動啟動的前向影子研究。每天開盤前用前一個已完成交易日資料凍結候選、排名、SKILL 與模型版本，保存四組意見、錯誤及費用；盤中可對照正式系統的研究結果。此工具不修改正式排程、策略或執行授權。

首週先檢查資料更新、來源、時間、模型完成率、研究延遲與額度消耗。五交易日標籤須等完整視窗成熟，通常不能在同一週末就對全部樣本評定收益。研究記錄不記為成交；如要核對真實可成交性，還需另外保存最新零股買賣報價、委託與成交證據。

正式接入前仍需補齊還原價／公司行動、歷史母體、公告時點、實際帳戶費用及資金交割；並取得較多前向結果支持。後續比較固定原始入場、目標、失效與期限，不因結果不理想而更換標籤。

## 驗證與保存

```powershell
python -m unittest discover -s tests -p test_selection_lab.py -v
python -m unittest discover -s tests -p test_multi_horizon_research.py -v
```

本次 23 項新測試與 21 項既有標籤測試通過，包含未來價格不影響歷史特徵、交易日缺口、未成熟視窗、未知成交金額、來源雜湊、模型分數身分、訓練切分、成本、CLI 呼叫額度／錯誤與不載入正式模組。另完成獨立環境 pip check、SKILL 格式驗證、PowerShell 解析及啟動器實跑。

版本摘要保存在 `reports/selection_lab_20261111/`。原始資料、完整逐筆觀測、模型、CLI 回覆與環境鎖定檔保留於本機 `research-runs/selection_lab_20261111/` 及 `research-runs/selection-forward/`。原始資料不提交，避免把資料服務的程式授權誤認為資料再散布授權。
