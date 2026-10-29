# H-002：近期支撐守住／收復的積極探索

2026-10-29新增 `h002-v1-20261029-exploratory`，與H-001及舊ATR情境並存。目的是把較短的支撐觀察交給Agent評估，不把三日穩定、長期固定錨當作唯一研究入口。這是在已知參考案例後提出的探索參數，不是樣本外績效或買點準確率。

## 參數與因果界線

`config/patterns.json` 的 `recent_support_reclaim` handler使用獨立 `H002Parameters`，不套用H-001參數。

| 參數 | 初值 | 用途 |
|---|---:|---|
| decline_lookback_sessions | 10 | 回撤參考高點窗口 |
| min_decline_fraction | 6% | 先有可辨識回撤 |
| local_low_lookback_sessions | 5 | 局部低點窗口 |
| holding_min_sessions | 1 | 至少一個後續完整交易日守住 |
| max_episode_sessions | 12 | 短期情境生命週期 |
| retest_distance_fraction | 4% | 日低距錨的回測接近範圍 |
| invalidation_buffer_fraction | 1% | 錨下固定失效下緣 |
| max_distance_fraction | 6% | 收盤距錨過遠拒絕追高 |
| volume_reference_sessions | 5 | 最新量對前五期平均量 |
| volume_contraction_ratio | 0.85 | 原始量縮提示；口徑未核實仍保留缺口 |
| min_reclaim_fraction | 0.5% | 收盤收復錨當日收盤的最小幅度 |
| min_guidance_raise_fraction | 5% | 公司自己上次預測至本次修正的比較 |
| catalyst_max_age_days | 7 | 可見公司預測修正的時效 |

錨在尚未有後續守住時可隨新低更新；凍結後不下移。後續日低跌破固定下緣，舊情境記INVALIDATED，當日不列正向機會。如果這根失效日同時是新的局部低點，下一個完整交易日才可建立另一份新假說並評估守住；保留舊失效episode及新舊錨身分，不把舊部位停損往下搬。

`SUPPORT_HELD`表示守住、回測距離及原始縮量提示符合；`SUPPORT_RECLAIMED`另有收復錨日收盤。這些是研究候選，並非確定支撐、勝率、正式交易資格或已成交。資料、公司事件及持倉原風險基準仍需各自驗證。未守住、量能不符、追高、基本面已核實惡化及固定支撐失效都有負向控制。

公司修正輸入為 `pattern_context.guidance_revisions`，每項有id、公告與可得時間、來源、期間指標、單位、前次公司預測及新預測。正向修正明示 `PREVIOUS_COMPANY_GUIDANCE_NOT_MARKET_CONSENSUS`，不生成市場一致預期或「超預期」標記；抽取未獨立核實則保留缺口。未知未來公告完全不進入早期輸出。

## 保存真實行情的前綴重算

沿用已保存Yahoo資料，沒有新網路或模型呼叫，H-001不變。這仍是今日版本歷史資料的事後重算，非歷史當時資訊集。

| 截點 | H-002結果 | 解釋 |
|---|---|---|
| 10/16 | VOLUME_REVIEW | 近期錨34,000，量比約0.908，未通過0.85 |
| 10/19 | INVALIDATED | 日低穿過34,000情境固定下緣 |
| 10/20 | VOLUME_REVIEW | 新錨32,420，量比約1.004 |
| 10/23 | INVALIDATED | 舊32,420情境失效；沒有當天立刻叫買 |
| 10/26 | SUPPORT_HELD | 新假說錨30,030，日低31,040、收31,070；距錨3.36%／3.46%，量比約0.714 |
| 10/27～29 | EXTENDED | 距30,030分別約12.75%、16.32%、23.31%，超過6% |

10/26新假說固定下緣為29,729.7、接近上緣31,231.2。這與舊ATR支撐無關；不能拿H-002的價位解除舊ATR失效或當成原持倉已授權的停損。單例能看出定義如何改變尋標結果，不能證明採用它會獲利。

驗證：`python -m unittest discover -s tests -p test_active_pattern.py -v`。全部輸入為人造控制，涵蓋未完成守住、量能、追高、跌破、獨立新episode、未來資料不影響前綴、基本面反證、公司上修非consensus及handler參數隔離。
