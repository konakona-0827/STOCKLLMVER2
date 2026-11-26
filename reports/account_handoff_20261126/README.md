# 交易電腦手冊與 Git 帳本交接驗證

2026-11-26。依使用者最新指示，金融帳本繼續放在原公開 Git 程式庫；`.env`、密碼、API keys、憑證及登入資料維持原本機使用方式。完整流程見 [TRADING_PC_TEST_RUNBOOK.md](../../TRADING_PC_TEST_RUNBOOK.md)。

## 本次交付

- 新手冊涵蓋取得分支、原帳戶／Git 快照兩條路徑、登入與 SDK、完成日線、群益盤中零股行情、資金核對、Check／只讀／Live、LLM 與正式委託成交驗收、紀錄及停止回退。
- `operations/account_transfer.py` 提供 export／inspect／restore；金融 SQLite 使用一致性備份，包含已提交 WAL，匯出時取得現有金融 owner leases 及兩個資料庫的寫入保留鎖。
- 只包含明列的金融資料庫與帳戶設定，檢查完整性、檔案 SHA、已知秘密值與 key 形態；舊快照不可覆寫，還原僅允許不存在的新目錄。
- parent 與本機 receipt 防止未承接最新快照的來源直接覆蓋交接。這不是跨電腦即時鎖或雙向資料合併；使用者須停止前一台，才讓下一台接手。
- 新增 14 項帳本移轉測試及 CI archive integrity 檢查；原 `execution/`、資金數值與實盤授權實作未改。

## 實際帳本快照

快照 `20261126T050725Z-eecf40bbac6c378f`，UTC 05:07:25／台灣 13:07:25／日本 14:07:25。7 個白名單檔案，壓縮檔 5,224,117 bytes。金融帳本包含 17 筆 execution_attempts、69 筆 execution_events、6 筆 ai_positions 資料列與原始對帳歷史；ai_positions 的資料列數不等同於正股數持倉數。

AI 累計上限 NT$10,000、最低保留 NT$5,000、單日買進上限 NT$30,000，買單預留 max（1%，NT$20），佣金最低 NT$1；來源設定完整保留，不把現有投入歸零。這是當時帳本備份，不代表當前券商持股或可用額，交易電腦仍須核對新行情與對帳。

## 驗證結果

| 驗證 | 結果 |
|---|---|
| 完整本機回歸 | 1,115 tests，747.704 秒，OK；[完整輸出](full_tests.txt) |
| 移轉專項 | 14 tests，OK；[輸出](transfer_tests.txt) |
| 真實帳本隔離還原 | 兩個資料庫，共 18 個資料表，所有資料列與來源一致；SQLite quick_check 均 ok |
| 實盤設定 | 還原檔與原 `execution_live.json` bytes 相同 |
| 機密與執行 | `.env` 未還原、原綁定未變、本次新送單 0 |

還原證據見 [roundtrip.json](roundtrip.json)。測試涵蓋已提交 WAL、部分成交與現金／資金上限、原停用授權保留、活躍 broker／dashboard／supervisor、資料庫寫入鎖、舊來源、現有目錄、竄改及路徑注入、損壞資料庫、密碼與 API key 拒絕匯出。

本次只進行工程測試、帳本備份與隔離還原，未啟動新 Skill Live、未呼叫新市場決策或送出真實驗證委託。實際盤中 LLM、群益新鮮度、受理、成交與獲利的驗收，依手冊由使用者在交易電腦執行；沒有把離線通過率當成盈利證據。遠端 CI 以該提交 GitHub Actions 狀態為準。
