# Japan Support Desk：日股支撐觀察與決策

由 `codex/support-desk-phase1` 延伸的獨立日股版本。分支 `codex/japan-support-desk-phase1`，工作目錄 `personalmono/japan-support-desk`；原台股 checkout 保持原分支。先做尋找標的、反證與可追溯決策，未包含券商、帳戶或訂單流程。

```mermaid
flowchart LR
    Y[Yahoo Finance 日線 API] --> D[JPX 日曆與資料閘門]
    D --> S[固定支撐掃描]
    S --> H[Head 分派有界任務]
    H --> A[Researcher 研究證據]
    H --> B[Reviewer 獨立反證]
    A --> G[Python 決策閘門]
    B --> G
    G --> L[SQLite 跨日固定支撐覆核]
    L --> R[可考慮 / 等待 / 不交易]
    R --> O[HTML 報告與重播快照]
```

## 執行

Python 3.11 以上，執行期不需要第三方套件。進入本工作目錄：

```powershell
python -m support_scout doctor
python -m support_scout japan-demo --out output/japan-demo
python -m support_scout observe --no-agents
python -m support_scout observe --no-agents --symbols 6857,8035,4063
```

`Observe.cmd` 預設日本，`OpenReport.cmd` 開最新完成報告。固定 20 檔觀察池在 `config/universe_jp.json`，包含 Advantest（6857.T）；不是全東證掃描，也不保證有合格標的。`--symbols` 可指定其他東京代碼，包括英數代碼。`--market TW` 保留原版相容入口。

每輪報告位於 `output/observations/<run_id>/report.html`。`latest.json` 指向已發布報告，`latest_attempt.json` 記錄最後嘗試；讀取時應同時查看狀態、資料日期與 `committed.json`。code 0 為觀察完成、2 為資料全部不可用／全域失敗、3 為部分資料或 agent 降級；0 不代表可以交易。

## 使用雙 Agent API

沿用原架構的 OpenAI Responses provider。研究員和反證員使用相同快照、不同上下文、各自請求；可以指定不同模型，這不是可帶工具遞迴行動的 agent。模型無行情抓取、shell 或券商工具。每輪最多 5 檔／10 次呼叫，只整對分派；無自動重試。

```powershell
$env:OPENAI_API_KEY = '你的金鑰'
$env:SUPPORT_DESK_MODEL = '你可用的模型 ID'
python -m support_scout doctor --require-agents
python -m support_scout observe --agents --max-packets 5 --max-calls 10
```

需要沿用自己已有的設定檔時，依 `local_settings.example.json` 建立忽略的 `local_settings.json`，明確指定 OpenAI 設定來源及兩方模型；不會自動讀其他專案憑證。金鑰不進入快照或報告。未設定模型時仍可跑行情與規則觀察；要求 agent 卻缺設定時明確降級，不冒充完成。真實模型呼叫會產生 API 費用。

## 決策含義

| 結論 | 意義 |
|---|---|
| `CONSIDER` 可考慮交易 | 支撐確認且接近、資料與背景完整、雙方有效審查、淨情境風險報酬及跨日檢查通過；交由人工評估 |
| `WAIT` 等待確認 | 型態尚未完成、資料或公司事件未知、審查未完成或跨日資料需核對 |
| `NO_TRADE` 目前不交易 | 支撐失效、反證否決、無可用型態、已追高或風險報酬不足 |

每檔保留研究資格 `RESEARCH/WATCH/REJECT`，並另外輸出交易研究結論，包含收盤參考、凍結支撐下緣、已知歷史阻力、情境成本後 RR、證據與下一步。參考收盤不是下一次可成交價格；支撐下緣是「日線收盤失效」條件，不是已設置或保證成交的停損。下個交易時段須重新驗證。

目前 Yahoo provider 固定 `observation_only=True`：未獨立核實公司行動、公司公告／決算、交易狀態與普通股分類，市場壓力亦未知。**目前真實 Yahoo 流程會輸出等待或不交易，不會直接升為 CONSIDER；即使兩個模型同意也一樣。** 完整的 CONSIDER 通道已以受控測試覆蓋，仍需下一階段補齊這些資料和策略驗證。這個狀態不能靠刪除 blocker 解決。

## 支撐模型與資料界線

保留既有因果模型：至少 60 根日線、前 20 日高點回撤 8%、左右各兩根較高低點後凍結 `低點 ± max(0.35 ATR14, 0.8% 低點)`；另日反彈 1.5 ATR，之後另日回測守住，後續收盤突破回測日高點。距支撐上緣超過 2.5 ATR 不追，收盤跌破凍結下緣即失效，不向下移動原支撐。規則尚未經樣本外驗證，不為了符合單一 Advantest 案例調參。

Yahoo OHLC 不與 adjusted close 混用；歷史調整語義仍未知。窗口內已知拆股會阻擋該檔。日圓成交值為 `close × volume` 估算，使用獨立日圓門檻。JST 15:30 收盤後保留至 16:00 發布緩衝，之前排除當日日 K；JPX 日曆已版本化至 2027 年，未知年份拒絕執行。漏日、過期、錯代碼、零量與來源失敗不以前值補齊。每次原始 JSON 保存 SHA256、取得時間與來源，今日下載的歷史資料不冒充歷史當時快照。

5% 當日帳戶虧損停工尚未實作，因為本階段沒有帳戶淨值、持倉與可信損益來源；不能用個股跌 5% 代替。Head 僅分派工作與執行固定檢查，沒有調高風險或下單的權限。

## 驗證與下一階段

```powershell
python -m unittest discover -s tests -v
python -m support_scout scan output/observations/RUN_ID/input.json --rules output/observations/RUN_ID/rules.json --out output/replay
```

重播還原當次資料與規則，不等於績效回測；重用當次 `reviews.json` 可驗證原 agent 裁決。未來資料不得改變既有截止的掃描。

下一步先補官方上市身分／公司行動／決算事件與市場背景，再做逐日走樣本外驗證及參數敏感度；確認券商後只替換行情 adapter，執行流程另外設計。暫不加入排程、Telegram、自動下單或帳戶風控。

[日股架構](docs/japan-architecture.md) · [Yahoo 資料介面](docs/japan-provider.md) · [決策政策](docs/japan-decision.md) · [日股獨立審查](docs/japan-review.md)。其他未冠 japan 的文件保留自台股基底，閱讀時以本 README 與日股文件為準。
