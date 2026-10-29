# 三個 Agent 的精簡 JSON 交換

`jp-agent-wire-v1` 保留尋標、研究、反證三個模型角色。完整行情、參數、來源證據與 canonical packet 留在本地稽核；API 收到由 Python 產生的精簡 view。這是傳輸格式，沒有新增第四個模型，也不以文字壓縮改寫支撐或交易閘門。

## 研究／反證輸入

`review_wire(packet)` 先驗證原 packet hash，回傳 `(view, aliases, metrics)`。view 保留截至時間、所有 `analysis_context`、synthetic／observation flags、公司與資料缺口、完整規則參數、ATR 技術特徵及 H-001 的獨立支撐／指標／參數。已確認持倉原樣放在 `portfolio_position`，不依建議猜測已成交。

日線使用 columns＋rows，保留最近15根及較早的重要日期：支撐anchor與左右窗口、識別日、反彈、回測、確認、H-001測試與凍結日期，以及可見歷史反證事件。價格不四捨五入，保留原 `closed_at`／`available_at`。所有證據的值與可得時間保留，舊失效／過期證據不因縮短歷史而消失。原始日線省略數量明示，模型不得聲稱看過省略資料；較長歷史的數值特徵仍由Python用完整可見歷史計算。

證據在單次請求內映射成 `e1`、`e2` 等別名；完整ID不需反覆傳輸。`expand_refs(answer, aliases, packet_id=..., symbol=...)` 會核對該別名表的packet／symbol綁定、表格完整性、未知及重複別名，再恢復canonical證據ID。別名不是可跨股票或跨快照重用的全域ID。

## 尋標輸入

`search_wire(packet)` 同樣先驗原搜尋封包。每個候選使用 `c1` 等別名，自己的證據另有 `e` 別名。數值特徵及支撐採共用columns與values陣列；H-001的參數與欄位、重複原因碼、取得時間均只送一次，數值保持原精度。催化公告時點、事前預期時點與比較值另保留在`event_facts`，不只傳已算好的stage。`analysis_context` 與ATR／H-001界線保留。SEARCH看的是計算特徵摘要，不宣稱檢閱整段歷史日線。

模型仍只回 `{selections:[{candidate_id,evidence_ids,rationale}]}`。`expand_search(answer,mapping,packet_id=...)` 恢復單次請求的候選及其所屬證據，重複／未知候選或證據整份拒絕。真正候選資格、名額與引用檢查仍由SEARCH既有validator執行，不因使用短ID放寬。

## 預算與量測

研究輸入使用 `max_input_bytes`，預設28,000 bytes；SEARCH可用獨立的 `search_max_input_bytes`，預設設定48,000 bytes。舊凍結policy沒有新欄時，搜尋回退使用原 `max_input_bytes`，不改寫其參數或版本。共同來源加入後必須再檢查最終input，超額受控失敗，不截候選、證據或缺口來冒充完整輸入。每輪有多少候選與API輸入預算是兩個不同限制。

`wire_metrics(original, final_view, policy, role='review'|'search')` 使用兩側相同的minified JSON編碼量測UTF-8 bytes，避免把pretty-print空白也算成策略節省。記錄角色、byte預算及減少比例。未安裝tokenizer時，`estimated_tokens` 保持null並明示無法估算；可注入本地 `token_counter` 取得估計。**bytes減少比例不是計費token減少比例**，實際模型tokens只採API回傳的`usage`，缺值不當成0。

2026-10-29無網路、無模型呼叫的同快照量測，明細存於忽略的 `output/communication-validation/metrics.json`：

| 輸入 | 完整 bytes | 精簡 bytes | bytes 減少 |
|---|---:|---:|---:|
| H-001合成support，研究／反證單檔 | 38,535 | 7,631 | 80.2% |
| H-001合成support，SEARCH單檔 | 9,630 | 4,510 | 53.2% |
| 保存的20檔真實行情，5個候選SEARCH | 61,802 | 12,049 | 80.5% |
| 同份保存行情中的6920.T研究輸入 | 62,520 | 11,268 | 82.0% |

後兩項只是以保存行情和目前規則重算來量測JSON大小，不是歷史當時策略或選股績效驗證。不同股票／事件數／持倉欄位會改變大小，不能直接外推固定每日費用。

## 回覆及共同來源

`bounded_schema` 限制summary與risks／missing_data數量及長度；`validate_wire_answer` 再作本地檢查，不能只依賴模型遵循schema。未知證據、重複別名與不符長度直接失敗，不默默裁掉模型內容。SEARCH rationale同樣受設定的item字數上限。

`shared_once` 可讓研究員檢索一次公開來源，再把同份來源抽取與缺口交給反證員。只共享facts，不共享研究員的summary／結論；反證員必須明示沒有另做獨立檢索。抽取仍可能有誤，不能自動把來源變成verified事件或解除PIT／公司行動阻擋。若需要真正獨立查找，可明確使用`independent`政策並接受額外搜尋用量。

所有對外文字仍是不可信資料，不能指示程式修改規則、回報成交或下單。精簡格式及別名hash提供本地一致性檢查，不是外部資料真實性的簽章。
