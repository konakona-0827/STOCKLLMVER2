# 日股日常操作（0.7.0）

在 `personalmono/japan-support-desk`、分支 `codex/japan-support-desk-phase1` 執行，Python 3.11以上。只讀已授權ENV的OpenAI欄位，不載入券商帳戶或下單模組。

## 觀察與頁面

1. `Doctor.cmd` 檢查行情與OpenAI設定，不顯示key。
2. `Observe.cmd --agents --max-packets 3 --max-calls 7`：固定可配置20檔、SEARCH選股、最多3對研究／反證。
3. `Dashboard.cmd` 啟動本機服務，再開 <http://127.0.0.1:8765>。`OpenReport.cmd` 則只看靜態當輪報告。
4. 核對資料日、資料拒絕原因、模型是否完成，再讀個股結論與支撐失效價。

目前是日線收盤研究。日本15:30收盤後仍保留到16:00的發布緩衝，之前排除當日日K；不是盤中即時掃描。日曆已版本化，未知年份及缺預期日資料拒絕，不以前值補。

`config/universe_jp.json` 保存固定研究池，目前max_symbols=20。使用者之後提供PayPay名單時可直接換symbols及version，或用 `--universe PATH` 讀另一份JSON；不預先聲稱這20檔券商可交易。`--universe-source jpx --scan-limit 100` 才啟用保留的廣域輪巡。

## 手動成交與持倉

本機頁面可填代號、BUY／SELL、股數、價格、費用、成交日期時間，選填固定失效價。只有勾「我確認已實際成交」才改變持倉；未確認記錄不算，建議也不算成交。按儲存後，再執行一輪分析才會使用新的結構化持倉。

頁面會保留成本與開倉日，供持有／減碼／退出評估；缺原風險基準時保持REVIEW。只補取既有池外持倉供退出研究，不擴大新進場池。這是人工宣告，不是券商對帳。詳見[持倉協定](manual-portfolio.md)。

## 通知目前暫緩

決策狀態變更會建立outbox事件；同狀態不重複，較舊報告不倒退覆寫。使用者要求稍後處理Gmail，目前本機enabled=false。既有驗證已寄1封，後續停用；不要因看到憑證檔存在就自行啟用。

```powershell
python -m support_scout notify-status
```

以後明確要啟用時，`notify-configure 地址` 設定同一寄收地址；`EmailSetup.cmd` 使用隱藏輸入存Google的16字元應用程式密碼，不是帳戶登入密碼。Google通常要求先開兩步驟驗證，某些帳戶政策不提供此功能：見[Google官方說明](https://support.google.com/accounts/answer/185833?hl=zh-Hant)及[應用程式密碼頁](https://myaccount.google.com/apppasswords)。不要在聊天中提供密碼。

憑證只在忽略的local_data；EmailSetup只儲存，不寄信。明確啟用且具憑證後，正式觀察發布會寄出待寄事件；也可 `notify-flush` 或按頁面寄送按鈕。SMTP認證失敗保留PENDING；傳輸結果不明為UNKNOWN，不自動重寄，需人工檢查。合成及歷史案例不排信，也不沖刷真實佇列。

## 用量與結果

SEARCH只從Python資料閘門及型態候選中提案，可不選；失敗不以數值topN替代。總呼叫上限包含SEARCH，剩餘只整對研究／反證，預設無自動重試。工具次數另計，無固定每日費用保證。

`config/communication.json` 預設精簡JSON、單次別名及shared_once：研究員檢索，反證員用相同事實獨立推理，不共享結論，也不冒稱再次檢索。API usage才是實際tokens；bytes減少比例另外呈現。

報告仍可能WAIT或NO_TRADE：Yahoo observation_only、公司行動／事件等未核實缺口不能由模型解除。現階段未開真實CONSIDER資格；程式通道有受控測試，但不代表可實盤交易。

exit 0為流程完成，2為全域失敗，3為資料／模型部分降級。查看 `output/observations/latest_attempt.json`、`latest.json`、當輪 `committed.json`，舊latest不能代表最後嘗試成功。2026-10-29固定20檔實測三角色成功、3檔公司行動待核，故DEGRADED。

## 案例與備份

```powershell
python -m support_scout advantest-case
python -m support_scout advantest-case-replay output/advantest-real-cases/RUN_ID
python -m unittest discover -s tests -q
```

ADVANTEST真實案例目前沒有抓到描述的機會，見[詳細差異](advantest-real-validation.md)。歷史重算保留今日來源版本與真正取得時間，不能當歷史當時的決策準確率。

備份 `local_data` 與 `output` 可保留原始資料、成交、通知、帳本及報告，兩者不進Git。沒有建立自動排程、券商連線或自動下單；5%帳戶停工仍待可信帳戶資料。
