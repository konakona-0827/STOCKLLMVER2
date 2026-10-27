# 第一階段架構與權限

2026-10-27 更新：已加入 `twse.py` 官方上市普通股資料、`observation.py` 每日入口、`ledger.py` 固定支撐跨日追蹤、`reporting.py` HTML 與 `settings.py` 本機健康檢查。詳見 [觀察操作](observation-runbook.md)。

公開原始價只走雙重 `observation_only=True` 通道；正式研究通道仍拒絕未核對公司行動的資料。觀察通道可計算暫定技術型態，但固定 context blockers 禁止任何模型或人工評語升為 RESEARCH。當前觀察不是歷史回測。

## 角色

| 角色 | 實作 | 可做的事 | 輸出 |
|---|---|---|---|
| Head of Desk | `desk.py`，確定性 Python | 依優先度分派、保留整對呼叫額度、彙整結果 | run ID、稽核、研究／觀察／拒絕名單 |
| Support Scanner | `support.py` | 核對日線順序、凍結支撐、辨識反彈與回測、更新失效狀態 | 日期、價位、證據 ID、未校準研究分數 |
| Researcher | 獨立模型請求 | 讀原始快照、說明支撐及公司論點、提出可推翻條件 | 結論、證據引用、風險與缺口 |
| Reviewer | 另一獨立請求 | 檢查同一快照的反例與錯誤，可否決 | 結論、證據引用、風險與缺口 |
| Gate | `agents.py`、`pipeline.py` | 重算資格、核對證據與兩方身分、阻擋缺失 | `RESEARCH / WATCH / REJECT` |

Head 無模型自由裁決、不下單。第一階段沒有 SNIPER／APPROVE、EXIT 持倉執行、RUG、錢包或券商工具。原貼文的「單日虧損 5% 停工」留待未來有可信帳戶損益與交易功能時實作，不能宣稱目前已提供帳戶風控。

不強求每天 20–40 筆；沒有合格標的可輸出空名單，不能為達件數降低門檻。

## 資料與快照

`contracts.py` 檢查台股普通股、完整日線、時間與交易日涵蓋、OHLC、股數與公司行動基準。`pipeline.py` 補市場、公司及近期事件的研究檢查。資料來源與 `point_in_time` 等欄位仍由資料提供者負責真實性，通過結構驗證不是外部查證。

`make_packet()` 深拷貝 core。SHA256 涵蓋 protocol、core、角色任務與指令；core 包含規則、synthetic 宣告、日曆來源與當時可見 sessions、原始可見日線及證據。未來日線不進入快照。每次 provider 前後都重建 canonical packet 檢查完整性；hash 不是外部來源簽章。

資料／任務變動就換 packet ID，舊評語不能沿用。JSON 輸出可往返重播；manifest 保存有效截止、全套規則及來源程式 hash。每日 episode 另依 symbol／規則 hash／價格口徑／anchor 日識別，不能用每天改變的 packet ID 取代。

## 呼叫與裁決

1. 先跑數值掃描，完整且接近支撐的技術候選優先，再看其他接近支撐的觀察；已遠離支撐的狀態排後面。明確失效／公司論點破壞不耗模型額度。
2. 每個標的預留兩次呼叫；單數剩餘額度不啟動半對。每輪上限由 `DeskConfig` 固定，模型無修改介面。
3. 研究員與審查員各取快照深拷貝、不同 instructions、無對方評語。每個標的最多兩個並行請求，無自動重試或多輪工具。
4. OpenAI Responses 使用 strict JSON Schema、`store=False`，未提供模型工具、shell、網路搜尋、帳戶或訂單。身分、packet ID、角色與產生時間由 runtime 填入，模型只回傳研究欄位。
5. `validate_reviews` 核對精確欄位、有效 evidence ID、必要證據、缺口與時間；研究結論須引用全部必要證據。任一有效否決為 `REJECT`；缺席、未完成或不合法評語維持 `WATCH`。JSON 的重複鍵與非有限數字一律拒絕。
6. 固定數值與背景檢查仍有優先權，模型雙方通過也不能讓追高、資料不足或近期重大事件的標的升級。

`RESEARCH` 是進一步研究資格。相同模型分開上下文仍可能同時犯錯；程式只驗證格式、引用存在及明確條件，不證明文字理由正確。手動 `scan --reviews` 的身分由匯入者自填，可信度與 runtime 綁定不同。

## 有界失敗與保存

API 每次 I/O timeout 為 1–60 秒，輸入 JSON 上限 512 KB、回應上限 2 MB、輸出 token 上限 256–8,000。這不是整輪硬性截止或美元費用上限；下一階段可加程序隔離與明確費用政策。預設每輪至多 5 檔／10 次，不重試。

模型拒答、不完整回應、超時、HTTP 錯誤、無效 JSON、假引用或快照被改，該方不算完成。Head 保留其他標的結果、錯誤及待處理名單；CLI code 3 表示代理降級。例外訊息不保存任意 provider 文字；API 回應錯誤本體、密鑰與 headers 不寫入報告。

稽核保留每次角色、runtime agent ID、模型 ID、時間、回應 ID、可得 token 用量與評語 digest。未回傳用量時保持未知。SQLite transaction 內先完整發布 run，成功才提交帳本；提交後寫 committed marker，再以程序鎖及截止比較更新 latest。失敗不把舊報告標成新結果。

## 官方 API 參考

Responses structured output 的格式與拒答／不完整回應依 [OpenAI 官方 Structured Outputs 文件](https://developers.openai.com/api/docs/guides/structured-outputs)。已完成假 HTTP 測試與真實雙模型研究呼叫。
