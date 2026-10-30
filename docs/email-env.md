# Gmail明確ENV憑證與決策摘要

2026-10-30續作：使用者確認完整欄位為 `GOOGLE_DEVEICE_PW`（保留原拼字），已支援 `--password-key GOOGLE_DEVEICE_PW`。只讀明確選定欄位，DEVICEPW作舊設定相容。已用此來源寄出一封正式繁中摘要，SMTP回覆成功，沒有自動回退舊檔。

郵件包含標的、結論、買進／等待條件、建議金額、換算股數、情境虧損與人工核對；同檔舊待寄狀態被SUPERSEDED保留稽核，不把過時建議當最新結果。見[部位試算](position-sizing.md)。

# Gmail DEVICEPW來源

使用者2026-10-29已明確重新授權啟用並確認Gmail寄送。原本「稍後處理」的停用要求由這次授權取代；寄收地址仍相同。

通知設定可另外保存 `secret_env_file`，明確指向使用者指定的原ENV；`password_env`設為`DEVICEPW`。程式只擷取該檔的`DEVICEPW`，不載入OpenAI、券商或其他欄位，也不把ENV內容匯入程序環境，不複製整份ENV或密碼。程式介面為：

```python
configure(address, path=DEFAULT_CONFIG, secret_env_file=authorized_env_path)
```

引用及密碼都只在忽略的本機設定／ENV，不能提交Git。只比對`DEVICEPW`這個欄位，欄名忽略大小寫及周圍空格；不猜其他密碼欄位。支援單引號、雙引號、`export`、註解及Google顯示用空格；不展開變數、跳脫或執行任何內容。`DEVICEPW`去除顯示空格後必須為16字元英數應用程式密碼；這不是Google登入密碼。重複定義、格式錯誤或找不到檔案都顯示受控狀態，不回退到舊檔偷偷使用其他憑證。

優先順序為設定的程序環境變數 → 明確ENV中的DEVICEPW → 僅在未設定明確ENV時沿用舊password_file。原schema_version=1設定不含`secret_env_file`仍相容。

`notify-status`只顯示READY／缺憑證／來源錯誤／停用，以及來源種類與布林就緒狀態；不回報密碼、ENV內容或檔案路徑。READY代表本機來源可用，**不代表已通過SMTP認證或郵件已送達**。實際寄送仍以outbox SENT／UNKNOWN與受控錯誤為準。

決策事件另保存`opportunity_decision`的action、label、pattern_id、reference_plan、manual_checks、policy_version；積極機會動作或區間變化可觸發新事件。若已有持倉，頂層動作仍優先使用持倉動作，避免將新進場研究冒充持倉操作。合成及歷史案例依原政策不排信、不沖刷待寄事件。

測試使用暫存檔與fake sender，不寄信：`python -m unittest discover -s tests -p test_email_env.py -q`。
