# 獨立審查紀錄

日期：2026-10-27（Asia/Tokyo）。範圍：台股支撐研究第一階段，不含策略績效驗證。

## 2026-10-28 分支覆核

覆核版本 `6d75af3`，Python 3.12.3。102 項 unittest 全數通過；另執行兩檔／四次呼叫的離線 `desk-demo`，以及 doctor。doctor 回報本機觀察與 OpenAI 設定就緒，執行權限為 false；這次覆核沒有新增真實模型或券商呼叫。

目前的「Subagent」能力是 Python Head 固定分派 Researcher、Reviewer 的獨立單次請求：各自使用相同快照的深拷貝、不同角色指令、不共享對方答案，身分由 runtime 綁定。可分別指定模型，但本機兩方目前設定為同一模型；它不是模型動態遞迴分派或帶工具的多輪子代理。

已核對角色隔離、快照完整性、整對額度、Reviewer 否決、模型失敗保留 WATCH，以及雙方同意仍無法解除硬性資料阻擋。靜態檢查及現有故障注入測試未發現阻擋目前日線觀察用途的新問題。每次 I/O timeout 仍不等同整輪硬截止，模型同源錯誤與研究文字正確性也未因此獲得保證。

重播本機保存的兩輪真實資料報告（`20261027T190626-3a77b1a039`、`20261027T191021-8ae5ac477c`）：輸入 digest、snapshot ID、逐檔裁決及 shortlist 均一致；原模型呼叫分別為 10／2 次，error count 均為 0，資料最新日為 2026-10-27。觀察 SQLite 的 `PRAGMA quick_check` 為 `ok`。

保留一項歷史稽核差異：19:06 那輪的資料庫 report_digest 對應加入 ledger 欄位前的報告，與最終 report.json 不同；移除 ledger 後可精確重現其舊 digest。19:10 那輪的 digest 與最終保存報告完全相同，現行 callback 後重算 digest 的程式及回歸測試也已存在。未改寫舊報告或帳本，不能將早期紀錄描述為完整的最終報告雜湊驗證。

分支與交易系統 develop 沒有共同歷史，應繼續以獨立專案使用。現有原始價、公司行動與事件資料缺口仍固定阻擋 RESEARCH，只適合既定的 OBSERVATION ONLY 用途。

## 實用觀察版本補充（0.3.0）

已擴充到 102 項離線測試，另以官方資料完成 19 檔、每檔 162 根日線的整批觀察；真實 OpenAI 有合成契約 smoke 2 次、真實行情第一輪 10 次及最終整合覆核 2 次，均無 provider error。最終覆核 collection COMPLETE、exit code 0、資料最新日 2026-10-27、帳本 report_digest 與保存 report.json 相符。

獨立 operational_review 檢查出並驗證修正：盤前缺昨日資料假完整、月初空月份、發布失敗留下孤立帳本、倒退資料覆寫 latest、callback 後稽核 digest、HTML attribute escaping，以及 doctor 的 SQLite 失敗。已加入故障注入與回歸測試。

新鮮度以台北 16:00 與官方年度假日獨立倒推，不用資料自己的尾日冒充預期日。報告發布先於 SQLite COMMIT，失敗回滾；完成 marker 及有鎖的 latest 指標在提交後才更新。

原始價通道永久保留 OBSERVATION ONLY blocker；這次驗證觀察流程可用，不證明支撐可靠或投資績效。以下保留第一階段審查紀錄。

開發由主代理整合；另有兩個 subagent：`support_method_review` 唯讀檢查支撐定義與因果順序，`architecture_reviewer` 獨立檢查資料、代理契約與執行器，並在修正後再次審查。兩者均未呼叫券商或讀取憑證。

## 已處理問題

| 問題 | 處理 | 證據 |
|---|---|---|
| 歷史鎖價日可能構成支撐確認證據 | pivot 左右窗口、反彈／回測／確認日有鎖價則阻擋技術候選 | 關鍵階段逐一鎖價測試，非關鍵日另保留對照 |
| 輸出日線 tuple 經 JSON 轉成 list 後無法完整重播比較 | 公開快照使用 JSON 原生 list／dict | CLI JSON 往返一致測試 |
| 快照引用原輸入 dict，可能在 packet ID 不變時被改 | 深拷貝 core，provider 前後重建 canonical packet | 修改原輸入、修改 provider 私有副本測試 |
| packet hash 未涵蓋任務、日曆與 synthetic | 納入 protocol、指令、日曆 provenance 與 synthetic | 三類篡改均拒絕 |
| agent ID 自填、評語時間可在遙遠未來 | runtime 指派角色、唯一 ID 與時間；手動匯入另驗上限 | 不同身分、執行區間、未來日期測試 |
| 頂層／日曆型別錯誤造成未封裝例外 | 明確 DataError | malformed input 測試 |
| 缺公司資料候選可能搶走完整候選的有限名額 | 完整技術候選優先，再候選與觀察佇列 | 8999／9001 名額競爭回歸測試 |
| JSON 重複 verdict 可能讓後值覆寫否決 | 所有 JSON 入口拒絕重複鍵及非有限數值 | 重複 verdict、巢狀重複鍵、NaN、1e999 測試 |

## 執行器驗證

- 56 項 unittest 通過，包含既有 36 項與新增 20 項。獨立審查員亦執行完整測試。
- 離線 `desk-demo` 完成分派、配對呼叫額度、WATCH 與 REJECT 裁決，以及快照／報告／評語／稽核輸出。
- 假 HTTP 傳輸核對 Responses endpoint、序列化、strict schema、store=False、不同角色 instructions、相同快照，以及沒有 previous_response_id 或工具。
- 模型拒答、不完整輸出、錯誤 JSON、假身分、缺 API key、任意例外、改動快照均無法讓標的升級。
- reviewer 可否決；雙方同意仍不能覆寫延伸過遠、資料缺口與公司事件阻擋。

審查員最後確認前述兩項執行器問題已修正，未見阻擋第一階段交付的問題；另提 JSON 指數溢位補強，已加 parse_float 有限值檢查及測試。

## 保留的研究限制

這一版只涵蓋回撤後反彈再測。嚴格低點、8% 回撤與 ATR 門檻尚未校準；等低雙底、突破回踩與趨勢高低點可能漏抓。縮量目前只是單一回測日／反彈日比較的輔助分數。

不同上下文仍可能產生相關錯誤；引用存在不證明文字推論正確。資料來源真實性、人工宣告、真實 API、台股行情 adapter、樣本外成效與成交條件均待後續驗證。這份獨立程式審查不等於投資模型已獲證實。
