# SEARCH 標的發掘層

`make_search_packet(initial_report, universe, max_candidates=40, max_selected=4)` 從已通過資料驗證的日本股票快照建立獨立候選池。`universe` 為具有 `version`、`id` 或 `universe_id`，以及 canonical `.T` 代號 `symbols` 的物件；整個物件保留 SHA256，範圍與版本一同綁定。

SEARCH 先看數值掃描形成的候選，剔除 `REJECT`、已遠離支撐與已失效型態；底部形成、反彈、回測及規則確認皆可交研究員發掘。候選先依確定性規則排序，最多提供 40 檔給模型，模型可以重排、少選或不選。它不負責買賣結論。未進入本輪行情掃描的全市場標的，也不在模型可憑空新增的範圍。

## 獨立上下文

每檔包含 canonical symbol、名稱、原研究 `packet_id`、技術狀態、資料阻擋與可見證據；不包含原始整段日線、其他 agent 的答案或交易結論。後續研究／反證員仍取原始完整快照，在自己的獨立上下文工作。SEARCH 的排序理由不能解除資料或風險閘門。

模型回應只能是：

```json
{"selections":[{"candidate_id":"原研究packet_id","evidence_ids":["同一候選的LATEST與ANCHOR證據"],"rationale":"為什麼值得獨立後續研究"}]}
```

模型不可回傳代號、價位或分數。程式驗證 ID、同候選引用、最新與 anchor 必要證據、唯一性與名額後，才映射回 canonical symbol。任何一項不合法都使整批 `FAILED`，不截取其中幾筆當成功，也不退回數值排序偷偷補名單。合法空選為 `ABSTAIN`。

## 執行與保存

`run_search(packet, provider)` 回傳 `status`、`selection`、已驗證 `answer`、`call_count`、模型／用量／稽核，以及完整 `search_packet`、packet digest 與 response digest。`selection` 每筆為 `{candidate_id, packet_id, symbol, evidence_ids, rationale, pre_rank, agent_order}`，分開保留數值預排與模型研究順序。

`OpenAISearchProvider(model, timeout=45, max_output_tokens=2000)` 使用一次 Responses strict JSON Schema、`store=False`，沒有 tools、上下文接續或重試。API key 只讀目前程序的 `OPENAI_API_KEY`，由主流程決定如何載入設定；不讀其他專案憑證。每次 I/O timeout 上限 60 秒，不宣稱是整輪硬性截止。`call_count` 為有界 provider 嘗試數，可能在實際 HTTP 之前因設定錯誤失敗。

`OfflineSearchProvider` 永遠空選，標為 `offline_simulation`、API 呼叫數 0；空候選池或名額為 0 也不呼叫模型。沒有模型實際搜尋時，不宣稱已完成標的發掘。

`validate_search_result(result, report)` 供派工與決策層再次檢查：packet hash、回應 digest、原 snapshot／截止／規則、各候選與現有完整研究 packet 的對映、證據及 canonical selection 均必須一致。失敗或空選回空名單；有成功宣告但資料被改寫時回受控 `DataError`，不派工。

本層不保存原始 API 回應／headers／金鑰；任意例外文字不入稽核。SEARCH 的 agent 身分與時間由 runtime 指派。獨立上下文仍可能與後續模型犯相關錯誤，發掘成功不代表支撐或交易報酬已驗證。

## 主流程關卡

預設 `observe --agents` 使用動態母池，先下載本輪行情、做確定性預掃，再執行SEARCH；有效selection才進入Head派工。下游模型只讀原行情快照。最終決策以雜湊core的 `discovery_required`／`source_universe_hash` 及已驗證SEARCH名單再次封關，不能只靠兩份研究評語跳過搜尋。`run_desk`公開入口也重驗，不會在搜尋失敗後花模型額度研究數值topN。

整輪max_calls包含SEARCH，名額上限同時受40檔候選上限與成對研究額度限制。空池／合法空選為ABSTAIN，失敗為FAILED，不同於成功找到標的；NUMERIC_ONLY明確代表沒有模型尋標。agent模式失敗不前進輪巡cursor，下一次可重試同批；無自動重試或背景排程。

Structured Outputs介面依 [OpenAI官方文件](https://developers.openai.com/api/docs/guides/structured-outputs) 核對。JSON結構合格後仍由程式驗證引用與候選，不能把schema合格當成投資論點正確。
