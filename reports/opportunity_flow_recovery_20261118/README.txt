2026-11-18 Windows 重開機恢復修正與驗證

基線：2a856563b1fb76d90342e8712dff05a18324deb5
分支：codex/new-system-tw-opportunity-flow

問題與證據
今日執行查核見 ../opportunity_flow_20261118_status/。交易電腦發生 Windows
更新重啟後，FLOW 程序不存在，原排程自動觸發數為 0，StartWhenAvailable
未啟用。當日研究及自動委託均為 0；凌晨成功帳務快照不代表目前帳戶狀態。
事件證據支持更新後沒有恢復，未記錄到精確的 Python 退出原因。

本次修正
1. 原 OpportunityFlow.cmd / RunOpportunityFlow.ps1 的正式 Live 入口自動安裝
   或修復恢復設定，再啟動與命令視窗分離的守護程序。無須另一套交易啟動器。
   只讀不註冊正式自動啟動。Headless 手動啟動保留前景 Ctrl+C 停止。
2. 完整排程包含開機、本人登入、每分鐘觸發；補跑開啟、重複忽略、無執行
   時間上限、不因電池模式停止、失敗每分鐘重試三次，不強殺執行程序。
   同名排程先備份再更新，已執行的券商 owner 不會被安裝器停止。
   權限不足時改建目前使用者 Startup 捷徑，收據明列實際僅有登入觸發；
   這個降級不具備完整排程的開機／每分鐘守護程序重啟能力。
3. 帳戶專屬守護鎖加上既有 Dashboard／券商鎖，防止不同 worktree、直接入口
   或排程重複啟動券商 owner。每 5 秒檢查子程序；意外退出可重啟，10 分鐘
   內失敗 3 次則退避，次數跨守護程序重啟保存。
4. 心跳區分 GUI 與 watch loop。心跳逾 180 秒、watch 180 秒未開始、或迴圈
   600 秒未前進時請求正常停止。逾 300 秒未退出列 STALLED_OWNER_WAITING，
   不強殺可能仍在金融動作中的程序、不另開第二個 owner。原 COM 快取只有
   在原 thread 確實死亡後才可更換；仍存活、隔離或正在停止均不算死亡。
5. 使用原 live 入口，重新向券商取得帳戶、庫存與未結委託後才恢復原流程。
   不重放舊 advice，不把舊帳務快照當成新授權。研究通過後仍自動複核、送單；
   券商確認成交後自動納入 AI 持股、持續管理及退出複核。
6. 手動停止、使用者關閉視窗及前景 Ctrl+C 保存暫停，排程不會覆蓋。Windows
   正在關機時保留原執行意圖；再次使用正式入口或按啟動 FLOW 才解除手動暫停。

風控
沒有改動策略政策、帳戶交易設定、ExecutionBridge 或原自動委託執行器。
最低保留額與 AI 資金上限仍是獨立參數；新買可用額仍取實際可用餘額、AI
剩餘配置與日額等條件中的限制值，扣除既有圈存。未結／不明委託、防重送、
資料時效、交易時段、雙方研究、下單前複核、費後報酬與損失風險均保留。
人工持股沒有轉交 AI 就不能被 AI 賣出。恢復不是無條件買進或保證成交。

驗證
完整離線回歸結果見 verification.json 與 regression.txt。外部網路及真實
Codex 模型停用，使用隔離帳本及模擬券商，未送出任何實盤測試單。

- 恢復狀態：手動暫停／毀損紀錄安全停止、舊帳戶快照不能變成新對帳、不同
  PID／run_id 不算存活、GUI 活著不能遮蔽研究迴圈停滯、精準程序重啟請求。
- 守護程序：意外退出重啟、重複守護與直接券商 owner 阻擋、不強殺、持久
  退避、原 live 命令不重放 advice、GUI 背景分離、Headless Ctrl+C。
- GUI：停止及再次啟動意圖、controller 意外死亡重試／退避、Windows 關機
  與使用者關閉的區分。
- 自動全流程：首次帳務查詢故障 -> 新對帳 -> 兩方研究 -> BUY 複核 -> 原
  execute_advice_file／原送單類別的模擬成交 -> 正式隔離 AI 帳本 -> 自動
  持股管理 -> SELL 複核 -> 原執行器模擬成交。測試不手動派發買賣，最後 AI
  股數歸零，另有 3 股人工股數保留。
- Windows 原生排程 COM 產生的 XML 已測試三個觸發、互動使用者、每分鐘
  無限重複、無儲存密碼及不強殺。開發電腦實際註冊回覆 AccessDenied，已
  實測建立目前使用者 Startup 捷徑，驗證 pythonw／參數並清理測試捷徑。
  沒有改動正式交易電腦排程。
- 真實隔離子程序實測：第一個程序退出，守護啟動第二個，第二個保存手動
  暫停後停止，啟動數 2、退出碼 0；無券商、模型及委託。
- PowerShell 正式啟動器 Check 通過：沿用正式帳本、相依套件齊備、ready=true，
  不登入券商、不送單。全套包含既有 UI 拆卸時的 Tk ThemeChanged 訊息，
  unittest 的 failures／errors 為 0。

交易電腦部署
在交易電腦拉取此分支更新後，照常啟動一次 OpportunityFlow.cmd，安裝器即
保存實際生效的觸發方式。若帳戶無排程註冊權限，登入啟動仍會安裝；要取得
完整開機與每分鐘排程，可用有權限的系統管理員身分執行同一入口一次。
後續 Windows 更新重開機，本人 Windows 帳戶登入後會恢復原正式流程。
券商 COM 與 Codex 憑證仍需在原使用者工作階段有效；未登入前不會交易。
沒有改 Windows 自動登入或更新政策，沒有新增儲存 Windows／券商密碼。

查核位置（原帳戶目錄，私人檔案不提交）
data/health/flow_recovery/task-registration.json：實際註冊方法及有效觸發。
data/health/flow_recovery/supervisor_status.json：程序、心跳、新對帳狀態。
data/health/flow_recovery/supervisor.jsonl：啟動、退出、恢復事件。
data/health/flow_recovery/intent.json：使用者是否要求持續執行。

驗證邊界
本次在開發電腦修正、測試及推送程式；未操作遠端交易電腦的 Windows，也未
實際重新啟動該電腦。已驗證自動交易程式路徑與故障恢復機制，不能把隔離
測試視為真實券商成交或實機重開機驗收。完整排程註冊成功與重開後新對帳
結果，以交易電腦上述收據及日誌為準。

Windows 行為依據（Microsoft）
https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks
https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/new-scheduledtasktrigger
https://learn.microsoft.com/en-us/windows/win32/taskschd/taskschedulerschema-restartonfailure-settingstype-element
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getsystemmetrics
