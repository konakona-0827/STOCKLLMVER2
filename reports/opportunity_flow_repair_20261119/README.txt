2026-11-19 券商會話、守護與公司事件恢復修正

基線：2ed2121c690aa1c53d3b5a78ef670761d37fbf05
分支：codex/new-system-tw-opportunity-flow
實機事故證據：../opportunity_flow_20261119_status/

查明及修正
1. rc=9999
   事故中健康回報已包含 session: CapitalReplySessionError: Reply reset failed:
   rc=9999，原 LiveCoordinator 卻只處理例外，未將這類回報升級為可恢復故障。
   現在回報也會封鎖新動作、清空舊提案；連續三次則要求目前程序正常退出。
   守護按原退避重新啟動同一 Live 入口，讓新程序建立新的 COM 會話並重新
   對帳。舊 owner 未確實退出前不建立第二個 owner，不把 SDK 的 9999 改成成功。
   成功對帳可清除連續失敗次數；庫存或對帳差異未核實不能解除既有阻擋。

2. 守護消失
   實機缺少守護退出原因，不能斷言是某一例外所致。原守護的日誌與狀態寫入
   錯誤可能直接中止監看，現改為旁路診斷，且異常退出／停止留下生命週期事件。
   FLOW 在心跳時確認帳戶專屬守護 OS 鎖（至少間隔 10 秒），確認守護未持鎖後
   補起新守護，再正常退出自身。新守護等待原 Dashboard／券商鎖釋放才啟動
   新程序；不靠過期 PID 或心跳欄位強殺金融程序。
   啟動入口也可對已知 PID／run_id 且心跳仍有效的孤立 FLOW 發出正常交接，
   不處理其他或無法辨識的 owner。使用者暫停與 Windows 關機時不補起守護。
   重啟前最後一份階段報告亦會保存，再建立下一次執行報告。

3. Windows 權限降級
   原先開機觸發註冊被拒就只剩 Startup 捷徑。現在先嘗試本人登入＋每分鐘
   的互動工作階段排程，仍被政策拒絕才退回捷徑。讀回設定須以 SID 核對本人；
   Windows 會將 principal／logon trigger 的 SID 正規化成不同格式的帳號名稱，
   直接比較字串會誤報失敗，已修正。收據只列實際生效的 3、2 或 1 種觸發。
   本機以隔離測試帳戶實際建立本人登入＋每分鐘排程、讀回驗證並清除；沒有
   更新正式交易電腦排程，沒有儲存密碼或變更 Windows 自動登入設定。

4. 公司事件及重複研究
   事故兩批三檔封包的可見價量／路徑不變，公司事件卻從 SOURCE_REVIEW_REQUIRED
   退回 NOT_CHECKED，進而產生新 material key。原因是事件集合只覆蓋當次行情
   批次且每次清空，輪替掉的公司遺失事件資料。
   現在先按公司代碼索引公告／公司行動，獨立覆蓋完整母體與持股，不隨行情
   輪替消失。失效或未檢查的來源不偽裝成正常，不使用舊事件取代新來源失敗；
   正式帳戶的付費研究會列 REVIEW_DEFERRED_EVENT_SOURCE 並等待來源恢復。
   凍結研究不被抹除，恢復相同事實後沿用快取，新公告等實質變化仍按原規則重審。

交易與風控
原資金保留額、獨立 AI 上限、日額、圈存、時段、研究、複核、費後與損失門檻、
人工持股歸屬及委託防重送保持原樣。沒有改 config、policy.json、原下單執行器、
ExecutionBridge、ExecutionStore、account_guard 或 pretrade_policy。
不明委託保持原帳本狀態，不清除 claim、不重送舊 advice、不新增強制撤單。
全流程仍是新對帳、研究、下單前複核、原執行器、券商確認後回寫與持股管理／賣出。

驗證（精確結果與 source hash 見 verification.json／regression.txt）
- 連續三次 rc=9999 要求正常重啟；成功重查重置次數；保留金不足不誤判為會話故障。
- 原對帳失敗測試的恢復 fixture 補齊庫存／對帳欄位，避免只憑 READY 與額度認定恢復。
- 不明派發 claim 跨新的 LiveCoordinator 仍阻擋重送，模擬原送單函式只呼叫一次。
- 原 watch、兩方研究、原 ExecutionBridge 與 execute_advice_file 完整測試兩種恢復：
  一般讀取故障，以及 rc=9999 三次後換新 coordinator／run_id／stop event；均自動
  BUY 複核、原執行器模擬成交、AI 帳本、持股管理、SELL 複核與模擬成交。最後 AI
  股數歸零，另保留 3 股人工股數，沒有手動派發買賣。
- 真實隔離子程序確認守護 OS 鎖消失後，啟動替代守護並正常交接；替代守護使用
  真正 supervise，只有原 broker／dashboard 鎖釋放後才啟動新子程序。
- 原守護仍持鎖、使用者暫停及 Windows 關機都不啟動第二個；不明 owner 不被要求停止。
- 守護日誌或狀態路徑不可寫時仍可監看，不呼叫 terminate／kill 處理券商 owner。
- 行情輪替不掉事件；無新事實第二次不增加模型呼叫；來源退化時不呼叫模型，來源
  恢復後重用有效 WATCH；公司代碼不混用，來源錯誤繼續列不可用。
- Windows 原生排程 ValidateOnly 與實際註冊／讀回／清理均納入全套；測試物件有
  唯一名稱及隔離帳戶路徑，清理會核對作用範圍，不操作正式工作。
- OpportunityFlow.cmd -Mode Check 實際通過，未登入實際券商或派實盤測試單。

原入口與部署
更新後仍使用 OpportunityFlow.cmd，不需要新增使用者參數或另跑修復／研究／送單
腳本。新守護會嘗試交接原先已知的孤立 FLOW；如果無法核對舊程序身分或舊金融
動作仍未結束，保留原 owner 並持續等待，不為完成驗收而強制中止。
本人 Windows 工作階段、券商及 Codex 憑證仍需有效。外部券商持續拒絕連線時，
只會退避重試並保持交易阻擋，不能保證外部服務一定恢復。新實機結果自動寫到
reports/opportunity_flow_runtime/ 及原帳戶的 runtime-audit.json。

本次在開發電腦及隔離券商／模型驗證，未操作交易電腦 Windows、未實際重開機、
未派實盤測試單。先前 rc=9999 的底層 SDK／網路原因與守護消失原因仍無完整實機
證據；本次修復的是可觀察、受控且不重送的自動恢復路徑，實機成效由更新後報告核對。

Windows API 依據
https://learn.microsoft.com/en-us/windows/win32/api/taskschd/nn-taskschd-itriggercollection
https://learn.microsoft.com/en-us/windows/win32/taskschd/logon-trigger-example--scripting-
