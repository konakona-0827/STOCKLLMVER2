Opportunity Flow 實機檢查與自動日誌匯出

交易電腦拉取更新後，照常雙擊一次 OpportunityFlow.cmd。
正式入口會自動安裝／修復自動啟動、啟動原 Live 流程並檢查實機；無須另跑
研究、驗收、日誌匯出或買賣腳本。啟動視窗最多等候 180 秒並列結果；背景
守護程序會繼續查核及更新報告，不會因超過啟動等候時間而停止交易流程。

11/19 補強：開機排程權限不足時先嘗試本人登入＋每分鐘排程，再退回登入
捷徑。FLOW 會確認守護的 OS 鎖，守護消失時補起新守護並正常交接自身，
舊券商 owner 未退出前不另開第二個。連續三次 rc=9999 也走正常退出、建立
新會話、新對帳的恢復路徑；原不明委託保持待核對，不清除、不重送。
事件資料不隨行情輪替消失；真正來源不可用時顯示公司事件待恢復，不重複
消耗模型呼叫。重啟前的最後階段也會保存至該次 runs 報告。

自動產生的檔案
- latest.txt：中文狀態、各檢查、觸發方式、資金參數及送單／成交計數。
- latest.json：機器可讀的實機事實與本次執行階段／最近 100 個事件。
- runs/<執行識別雜湊>.json：各次子程序執行的最後報告，供重啟前後比對。
- 原帳戶目錄 data/health/flow_recovery/runtime-audit.json：相同報告的副本。

每 60 秒讀回目前 Windows 排程／登入捷徑；每 30 秒或狀態改變時保存。
排程檢查不只讀先前的安裝收據。帳戶與研究證據由本次執行中的原流程提供，
不得用上一次程序的 PID、run_id、版本、舊對帳或舊研究紀錄認定新啟動通過。
完整 Windows 排程需要有權限註冊；本人排程列 OWNER_LOGON、EVERY_MINUTE，
Startup 捷徑降級則只列 OWNER_LOGON。
券商仍需要本人 Windows 帳戶已登入，不保存自動登入密碼。

主要狀態
AUTOMATION_ACTIVE：啟動設定有效、本次程序／版本／新對帳／研究迴圈確認，
且已觀測數值研究掃描；正式複核與送單仍受原條件限制。
WAITING_RESEARCH_WINDOW：新對帳及自動流程確認，盤外等待研究時段。
WAITING_DAILY_DATA：原流程正在等候官方日線資料補齊。
AWAITING_FRESH_ACCOUNT：本次啟動後尚無有效新券商對帳，不視為通過。
AWAITING_RESEARCH_CYCLE：自動流程已啟動，尚未觀測到本次研究掃描完成。
RESEARCH_BLOCKED：本次較新的資料／研究錯誤，不能用先前成功掃描遮蔽。
STARTUP_CONFIGURATION_BLOCKED：目前實際自動啟動設定未通過。
STARTUP_INSPECTION_UNAVAILABLE：Windows 設定讀回失敗，不能當成已安裝。
RUNNING_VERSION_UNVERIFIED：無法確認目前程序與守護版本相同。
EXISTING_ACCOUNT_OWNER：原帳戶已有執行者，不建立第二個券商 owner。
MANUALLY_PAUSED：使用者停止或正常關閉，排程不會擅自恢復。
RECOVERING / STALLED_OWNER_WAITING：請求原程序正常停止；仍存活的金融
動作不會被強殺，不明委託不會被重送。
INSTALLATION_FAILED / LAUNCH_FAILED：安裝或啟動失敗，不能認定已運行。

研究、下單前複核、原執行器派發、送單回報、確認成交及持股管理分別記錄。
委託回覆不明另列 unknown_events，不能因未取得確認就當成沒有送單；仍須
依原帳本重新對帳，不自動重送。此計數保留本次曾發生的不明事件。
研究掃描可能沒有符合條件的候選；程式不會為驗收而改條件、模擬實盤成交、
強制進場或移除風控。無 AI 持股時的持股管理事件只表示已執行管理檢查。
資金欄位需配合對帳時間；最低保留額與 AI 上限各自獨立，保留原資金規則。

匯出內容採白名單，不包含帳號、憑證、私人路徑、模型原文及原始券商回覆。
檢查匯出失敗不改變交易結果、不導致重送。Git 目錄不可寫時，帳戶副本列出
REPOSITORY_EXPORT_UNAVAILABLE。此目錄的產生檔可隨原本 Git 提交一併推送；
啟動器不自動 commit 或 push。尚未在交易電腦產生的報告不會預填成功結果。
