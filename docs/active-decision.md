# ACTIVE_MANUAL：三角色的條件式試單評估

0.8.0依2026-10-29使用者要求新增，仍只有SEARCH、研究、反證三個模型角色。不是把案例答案寫入prompt，也沒有自動下單。H-001及原ATR正式資格保留；H-002另提早期支撐機會。

## 參數與交換

`config/opportunity.json` 凍結到bundle、canonical core、SEARCH及report，模型另收到Python算好的 `opportunity_reference_plan`。精簡JSON保留這些參數及引用，沒有為節省tokens隱藏舊失效情境或更近供給。

| 參數 | 初值 | 定義 |
|---|---:|---|
| max_distance_fraction | 6% | 收盤距H-002錨的最高距離 |
| max_risk_fraction | 6% | 至H-002下緣，加成本與跳空情境的風險比例 |
| minimum_net_rr | 1.5 | 至選定主要阻力的成本後風報門檻 |
| target_lookback_sessions | 20 | 只看錨前已可見歷史 |
| resistance_pivot_left/right | 1/1 | 已確認局部高點；選高於參考價且最近價位的pivot |
| entry_cost_fraction | 0.2% | 進場研究成本假設，非券商報價 |
| exit_cost_fraction | 0.2% | 出場研究成本假設 |
| gap_buffer_fraction | 0.5% | 情境緩衝，不保證涵蓋實際跳空 |

無可用pivot時用錨前最近5期高點，來源窗口不足不建立計畫。保存實際窗口數、候選阻力與錨後較近供給高點。主要pivot不是途中唯一阻力，更近供給可能使反彈提前停止；模型必須一起評估，不能把較遠目標當預測或保證。

模型在同一次回覆加上：

```json
{"opportunity_view":{"action":"PROBE_BUY_REVIEW|WAIT|AVOID","pattern_id":"H-002","reason":"條件、反例與待人工核對"}}
```

沿用本次top-level evidence_ids；正向評估必引H-002所有required證據。這個view與formal verdict分開：來源仍缺核實時formal可WATCH，獨立機會可有條件支持；不是藉改RESEARCH語義解除舊硬閘門。初次實測兩方混填RESEARCH且有missing_data，被validator拒絕，已修正任務說明，沒有放寬validator或強制買進。

## 程式裁決

只有有效SEARCH選中、H-002有效、兩方opportunity_view皆PROBE_BUY_REVIEW、距離／風報通過才顯示條件式試單。任一AVOID否決；缺一方、格式／引用無效、公開來源被排除或未完成均等待。公司論點破壞、追高或支撐失效不進場。資料修訂、帳本倒退／錯誤及來源池改寫阻擋；舊ATR情境失效本身不冒充新H-002失效。

輸出保留 `is_trade_approval=false`、`execution_enabled=false` 及所有人工核對項目，包含資料口徑、公司事件、券商資格與下一時段價格。H-002以後續日線最低價穿過固定下緣判失效；這是事後可見的日線提醒，不是即時或保證成交的停損。既有手動持倉的MANUAL風險基準仍依使用者記錄，不能自行換成H-002或下移。

本機頁面會並列積極評估與原正式資格。沒有持倉時積極正向對應ENTRY_REVIEW，只有手動確認成交才建立持倉；已有持倉仍優先持有／退出評估，不把試單研究當加碼。通知包含機會動作、版本、區間與核對項目。

## ADVANTEST探索結果

10/26的H-002以10/23低點30,030為新假說，固定下緣29,729.7，上緣31,231.2。日低31,040、收31,070，量比約0.714；前提只有一個後續交易日守住，尚非多次確認支撐。

最近的錨前已確認pivot參考35,420，情境風險約5.21%、成本與跳空後RR約2.51。同時保留錨日高點32,610及10/26高點32,370的較近供給；不能宣稱能直接到35,420。公司上修與市場超預期仍分開，沒有consensus不捏造超預期。

三角色真實API重測：SEARCH選中10/26，研究與反證均支持條件式試單，formal仍NO_TRADE；10/27與10/29因超過6%距離而AVOID、SEARCH空選。保存完整JSON、角色短評、usage及離線重播。這是已知案例促成的探索參數，規則作者已見後續走勢，不能當成歷史當時決策、命中率或樣本外績效。

較早版本只用H-001漏抓的紀錄完整保留，不回寫為成功。後續應在未見標的、失敗支撐與不同市場狀態評估，再決定是否改門檻。14項人工控制測試覆蓋雙方共識／否決、缺交接、風報／追高、證據與來源竄改、修訂／倒退及模擬隔離；H-002另有10項因果與失效測試。

OpenAI回覆仍採 [Structured Outputs官方格式](https://developers.openai.com/api/docs/guides/structured-outputs)，並在本地驗證語義及引用；schema符合不代表推論正確。
