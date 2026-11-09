"""Local presentation labels only; never changes model packets or decisions."""
from html import escape
import json


# The same key retains its raw spelling so archived JSON remains easy to audit.
LABELS = {}
for line in '''
symbol|股票代號
name|名稱
exchange|交易所
currency|幣別
packet_id|證據封包識別碼
snapshot_packet_id|來源封包識別碼
snapshot_id|快照識別碼
data_status|資料狀態
price_basis|價格口徑
observation_only|僅供觀察
technical|技術分析（ATR支撐通道）
state|狀態
position|位置或已確認持倉
bar_count|完整日線根數
latest_session|最新完整日線日期
latest_close|最新完整日線收盤價
support|此通道的支撐區
anchor_at|支撐錨點日期
anchor_session|支撐錨點交易日
detected_at|型態被辨識日期
available_at|這份證據可取得時間
anchor|支撐錨點價格
lower|支撐區下緣
upper|支撐區上緣
atr|平均真實波幅（價格單位）
drawdown_at_anchor|到錨點的回落比例
evidence_ids|引用證據編號
bounce_at|反彈日期
retest_at|回測日期
confirmed_at|確認日期
invalidated_at|失效日期
retest_high|回測當日高點
distance_above_zone_atr|收盤高於支撐上緣的ATR倍數
average_turnover_20_jpy|近20日平均估算成交值（日圓）
turnover_currency|成交值幣別
retest_vs_bounce_volume|回測量／反彈量
priority_components|研究排序分數組成
proximity|接近支撐分數
drying_retest_volume|回測縮量分數
latest_close_position|收盤位置分數
priority_score|研究優先分數
score_meaning|分數含義
technical_candidate|通過此通道技術候選條件
blockers|尚未通過的條件
invalidation_distance_fraction|到失效價的距離比例
stress_scenario|假設壓力情境
assumption|情境假設
price|價格
below_invalidation|情境價低於失效價
evidence|完整證據
id|識別碼
kind|證據種類
session|交易日
values|證據數值
pivot_session|轉折交易日
pivot_low|轉折低點
atr_at_detection|辨識時的ATR
previous_high|此前高點
pivot_right_sessions|確認轉折所需後續交易日數
close|收盤價
threshold|判斷門檻
frozen_lower|凍結下緣
frozen_upper|凍結上緣
closed_at|該日線收盤時間
open|開盤價
high|最高價
low|最低價
volume|成交股數
turnover_twd|沿用格式的台幣成交值欄（非日圓）
turnover_jpy|估算成交值（日圓）
halted|停牌標記
limit_locked|鎖價標記
latest_evidence_id|最新日線證據編號
episodes|歷次型態區段
context_blockers|公告與市場背景待查項目
agent_reviews|研究與反證評語
agent_errors|Agent執行錯誤
verdict|研究裁決
reasons|原因
patterns|各個獨立機會型態
pattern_id|型態編號
version|規則版本
definition_digest|定義校驗碼
definition|型態定義
handler|型態計算方式
enabled|是否啟用
hypotheses|待驗證的假說
evaluation_horizons_sessions|評估期限（交易日）
agent_guidance|Agent判讀指引
parameters|規則參數
decline_lookback_sessions|回落計算回看日數
min_decline_fraction|最小回落比例
stabilization_min_sessions|最少穩定日數
stabilization_window_sessions|穩定觀察窗口日數
max_stabilization_range_fraction|穩定區間最大振幅比例
historical_lookback_sessions|歷史回看日數
min_support_tests|最少支撐測試次數
test_separation_sessions|兩次支撐測試最小間隔
support_band_fraction|支撐帶寬比例
volume_reference_sessions|參考量能交易日（參數中為日數）
volume_contraction_ratio|測試量／參考量
max_distance_fraction|最大離支撐距離比例
breakout_lookback_sessions|突破回看日數
breakout_volume_multiple|突破量／參考量倍數
catalyst_max_age_days|催化事件最長有效天數
min_surprise_fraction|超預期最小幅度
max_episode_sessions|型態最長有效交易日數
stage|型態階段代碼
stage_label|型態階段說明
research_candidate|符合該型態的研究候選條件
frozen_at|支撐固定日期
metrics|實測指標
decline_fraction|回落比例
no_new_low_sessions|未再創新低的交易日數
stabilization_range_fraction|穩定區間振幅比例
support_test_sessions|支撐測試交易日
support_test_count|支撐測試次數
volume_basis_comparable|量能口徑已確認可比較
breakout|已符合突破條件
distance_from_support_fraction|距支撐錨點比例
positive_catalyst_evidence_ids|正向催化證據編號
post_event_price_available|已有事件公布後的價格證據
decline_driver|下跌原因分類
required_evidence_ids|必要證據編號
reason_codes|原因代碼
empirical_status|實證驗證狀態
execution_enabled|允許程式下單
stable_sessions|穩定交易日數
terminal|該型態區段已結束
limitations|使用限制
local_low_lookback_sessions|近期低點回看日數
holding_min_sessions|低點後最少守住日數
retest_distance_fraction|回測允許距離比例
invalidation_buffer_fraction|失效緩衝比例
min_reclaim_fraction|最小站回幅度
min_guidance_raise_fraction|公司展望上修最小幅度
episode_id|型態區段編號
invalidation_basis|失效判斷依據
raw_volume_contraction_observed|原始數據出現縮量
latest_low_distance_fraction|最新低點距錨點比例
anchor_close|錨點當日收盤價
reclaimed_anchor_close|已站回錨點收盤價
company_guidance_raise_evidence_ids|公司展望上修證據編號
market_consensus_available|取得市場事前預期
prior_failed_episode_count|此前失敗型態區段數
failed_daily_low|導致失效的日線低點
scope|適用範圍
holding_sessions|觀察／持有期限（交易日）
new_episode_after_failed_anchor|前一錨點失敗後的新區段日期
setup_kind|買入型態種類
trend_window_sessions|趨勢均線日數
short_window_sessions|短均線日數
signal_window_sessions|訊號觀察日數
volume_window_sessions|量能觀察日數
min_relative_volume|最小相對量
min_momentum_fraction|最小動能比例
max_momentum_fraction|最大動能比例
max_daily_gain_fraction|最大單日漲幅比例
min_close_location|最低收盤位置比
max_pullback_fraction|最大回檔比例
reclaim_buffer_fraction|站回門檻緩衝比例
max_trigger_gap_fraction|觸發價最大距離比例
max_opening_gap_fraction|開盤追價最大比例
stop_price_fraction|短線戰術風險距離比例
minimum_turnover_jpy|最低估算成交值（日圓）
min_horizon_range_fraction|最小期限內振幅比例
risk_level_meaning|風險線含義
structural_recent_low|近期結構低點
ma_short|短期移動平均價
ma_long|長期移動平均價
relative_volume|相對成交量倍數
momentum_fraction|動能報酬比例
daily_return_fraction|單日價格報酬比例
close_location|收盤在當日高低區間的位置比
pullback_fraction|回檔比例
horizon_range_fraction|期限內價格振幅比例
atr_fraction|ATR占股價比例
average_turnover_jpy|平均估算成交值（日圓）
entry_trigger_reference|進場觸發參考價
entry_trigger_required|需要等待觸發
trigger_required|需要等待觸發
entry_ceiling_reference|進場價格上限
watch_trigger_reference|觀察用站回參考價
previous_close|前一交易日收盤價
trigger_reference|觸發參考價
buy_type|買入類型代碼
buy_type_label|買入類型說明
market_quote|Yahoo延遲參考報價
quoted_at|來源報價時間
fetched_at|本機取得報價時間
source_url|資料來源網址
source_sha256|來源內容校驗碼
age_seconds|報價距快照的秒數
usable|本輪報價可用
is_realtime|是否即時報價
meaning|含義與限制
trade_decision|原正式資格裁決（獨立通道）
conclusion|結論
label|中文結論
summary|判斷摘要
next_steps|後續待確認事項
research_verdict|研究裁決
technical_blockers|技術阻擋條件
data_errors|資料錯誤
as_of|本輪決策快照截止時間
policy|決策政策
minimum_net_rr|最低成本後報酬／風險比
roundtrip_cost_bps|買賣合計成本假設（基點）
gap_buffer_atr|跳空緩衝ATR倍數
resistance_lookback_sessions|阻力回看日數
reference_plan|此通道的價位參考計畫
reference_session|參考日線日期
reference_available_at|參考資料取得時間
entry_reference|評估進場參考價
entry_basis|進場價格依據
entry_price_basis|進場價格口徑
invalidation_reference|失效／戰術風險參考價
invalidation_rule|失效規則
invalidation_note|失效規則補充
target_reference|目標參考價
target_basis|目標計算依據
target_evidence|目標價證據
window_start|回看窗口開始日
window_end|回看窗口結束日
source|資料來源
gross_reward_risk|未扣成本的報酬／風險比
net_reward_risk|成本與風險情境後報酬／風險比
cost_assumptions|成本情境假設
broker_costs_verified|券商成本已核實
validity|價位有效範圍
revalidation_required|下輪需重驗事項
roundtrip_cost_per_share|每股買賣成本假設
gap_buffer_per_share|每股跳空緩衝
nominal_risk_per_share|每股價差風險
stressed_loss_per_share|每股壓力情境虧損
net_reward_per_share|每股成本後目標收益
account_loss_guard|帳戶虧損保護資料狀態
opportunity_decision|目前採用的積極人工機會評估
action|建議動作
candidate|是否為研究候選
policy_version|機會政策版本
agent_views|兩方機會評語
channel|型態通道
risk_fraction|計畫風險比例
selection_reward_risk|選股用報酬／風險比
minimum_reward_risk|最低選股風報比
exclude_broker_costs_for_selection|選股門檻不計券商成本
target_return_fraction|策略價格目標漲幅
gross_target_fraction|價差目標報酬比例
estimated_net_return_fraction|成本後估算報酬比例
break_even_win_rate_scenario|此報酬風險假設的損益兩平勝率
time_exit_sessions|最長持有後重新評估退出日數
entry_cost_fraction|進場成本假設比例
exit_cost_fraction|退出成本假設比例
nearby_supply|較近供給／阻力區
target_limitation|目標價使用限制
reprice_before_entry|實際進場前需重算價位
target_reference_at_entry_ceiling|以上限進場重算的目標價
invalidation_reference_at_entry_ceiling|以上限進場重算的風險價
manual_checks|操作前人工核對
formal_qualification|原正式交易資格
is_trade_approval|是否已批准交易
alternatives|其他買入型態候選
research_status|研究完成程度
volatility_watch|僅為高波動觀察
manual_action|已確認持倉／人工操作評估
agent_position_views|兩方持倉評語
entry_qualification|進場資格
filled|已確認實際成交
execution_plan|Python部位與人工執行試算
entry_condition|可評估進場的條件
suggested_amount_jpy|建議投入金額（日圓）
suggested_shares|按金額換算的估計股數
sizing_status|部位試算狀態
risk_budget_jpy|單筆情境虧損預算（日圓）
estimated_loss_jpy|建議部位的情境虧損（日圓）
effective_risk_fraction|納入成本及跳空的部位風險比例
broker_adjusted_net_rr|券商成本情境後風報比
broker_mode|部位計算模式
held_quantity|目前已確認持股數
quantity_meaning|股數換算限制
researcher|研究員
reviewer|反證員
risks|風險
missing_data|缺少資料
role|角色
agent_id|Agent識別碼
produced_at|評語產生時間
public_information|公開資訊查找結果
max_tool_calls_per_review|每次研究最多工具呼叫
max_findings_per_review|每次研究最多資訊筆數
max_age_days|來源最長有效天數
max_cutoff_lag_hours|截止時間最大落差時數
retrieved_at|來源取得時間
status|執行／查核狀態
completed_tool_call_count|完成的工具呼叫次數
tool_attempt_count|工具嘗試次數
tool_calls|工具呼叫紀錄
type|種類
queries|搜尋詞清單
query|搜尋詞
sources|來源清單
url|來源網址
title|標題
citations|引用紀錄
raw_findings|未篩選的模型抽取資訊
category|資訊分類
source_type|來源種類
published_at|來源發布時間
time_precision|發布時間精確程度
fact|來源事實摘要
period|對應財報期間
expectation_basis|市場事前預期依據
implication|資訊方向
findings|通過格式與時點檢查的資訊
rejected_findings|被排除的資訊
reason|原因
finding_digest|資訊校驗碼
gaps|尚未核實的缺口
claim_status|資訊核實程度
clears_context_blockers|是否能解除背景阻擋
digest|校驗碼
opportunity_view|該角色的機會意見
quantity|已確認股數
average_cost|持倉平均成本
opened_at|首次開倉時間
fees|實際費用
risk_reference|已確認持倉的固定風險基準
defined_at|風險基準設定時間
confirmed|已確認實際成交
side|買賣方向
filled_at|實際成交時間
'''.strip().splitlines():
    key, label = line.split('|', 1)
    LABELS[key] = label

HELP = {
    'priority_score': '只用於安排研究先後，不能解讀為上漲機率或勝率。',
    'market_quote': '延遲價與完整日線分開。先看 quoted_at；usable 僅表示本輪通過時間檢查，之後仍會過期。',
    'age_seconds': '以這輪 as_of 計算，不會隨著你開著頁面自動增加；看目前報價年齡需比對現在時間。',
    'latest_close': '最近一根已完成日線收盤；盤內可能仍是前一交易日，不是目前成交價。',
    'opportunity_decision': '日常先看此結論，再看 execution_plan 金額與條件；candidate=true 本身不代表買進。',
    'trade_decision': '較嚴格的原正式資格通道，與積極人工機會評估分開，勿混用兩套支撐／風險價。',
    'entry_reference': '試算基準，不是訂單或保證成交價；WAIT 即使有此數字也不是買訊。',
    'entry_trigger_reference': '先達到此價才重新評估；突破價格本身不會跳過反證、風控與來源時間檢查。',
    'entry_ceiling_reference': '可重新評估的價格上限，並非整個區間無條件可買；實際買價需重算目標與風險線。',
    'invalidation_reference': '僅屬本通道的失效或戰術風險線；不是已送出的停損單，跳空可能超過。',
    'target_reference': '策略目標，不是價格預測；短線通常依實際進場價重算 +2% 股價目標。',
    'suggested_amount_jpy': '只有有效操作提案且預算、風險通過才給正數。0 表示本輪不建議新增買入。',
    'suggested_shares': '金額除以估計價格的換算值；不是券商可成交股數或已持有股數。',
    'estimated_loss_jpy': '依風險價、成本與跳空緩衝估算的情境損失；不是最大虧損保證。',
    'target_return_fraction': '0.02 = 股價目標 +2%；依目前選股設定不代表扣成本後淨賺2%。',
    'estimated_net_return_fraction': '依保存的成本假設估算，與股價漲幅分開；尚非實際已實現收益。',
    'break_even_win_rate_scenario': '依假設盈虧計算的損益兩平門檻，不是模型預測或經驗證成功率。',
    'selection_reward_risk': '例如 1.5 表示目標價差是風險價差的1.5倍；不代表勝率。',
    'net_reward_risk': '在這套計畫的成本與跳空假設下計算，不與另一通道的價位混用。',
    'close_location': '0 表示收在當日最低、1 表示收在最高；當日沒有價差時依程式預設處理。',
    'volume_contraction_ratio': '小於1表示測試時成交量較參考量少；仍要確認量能口徑可比較。',
    'execution_enabled': '本專案沒有券商下單能力，此欄應為 false。',
    'holding_sessions': '單位為交易日，休市不算；依所在通道可表示守住日數或持有評估期限。',
    'risk_budget_jpy': '你設定的本金乘單筆風險比例；不代表建議每次用滿風險預算。',
    'research_status': '未研究與完成後否決不同；固定20檔會預掃，僅尋標提交的少數進一步研究／反證。',
    'clears_context_blockers': '模型抽取的公開資訊不自動視為獨立核實。',
}

VALUES = {
    'PENDING_EXECUTION_DETAILS_NO_ADD':'已回報買入金額，成交明細待補，暫不加碼',
    'USER_REPORTED_POSITION_DETAILS_PENDING':'使用者已回報買入，成交股數與時間仍待補齊',
    'WATCH': '觀察', 'WAIT': '等待', 'AVOID': '暫不進場', 'PROBE_BUY_REVIEW': '條件式人工試單評估',
    'CONSIDER': '可考慮', 'NO_TRADE': '不交易', 'REJECT': '否決', 'SUPPORT': '支持',
    'HOLD': '續抱評估', 'REDUCE_REVIEW': '評估減碼', 'EXIT_REVIEW': '評估退出', 'REVIEW': '待人工核對',
    'BUY': '買進', 'SELL': '賣出', 'REVIEWED': '已完成研究／反證', 'AWAITING_REVIEW': '待研究／反證',
    'NUMERIC_SCAN_ONLY': '僅完成數值掃描', 'NO_NEW_BUY_SIGNAL': '沒有新的買入訊號',
    'HOLDING_REVIEW_NO_ADD': '已有持倉，本輪不加碼', 'BUDGET_NOT_CONFIGURED': '尚未設定資金與風險',
    'INVALID_REFERENCE_PLAN': '參考價位不完整或無效', 'BROKER_COST_RISK_REVIEW_REQUIRED': '成本與風險需核對',
    'BELOW_MINIMUM_OR_LOT_UNAFFORDABLE': '低於最低金額或整股單位無法負擔',
    'PORTFOLIO_INTEGRITY_REVIEW_REQUIRED': '持倉資料完整性待核對', 'SIMULATION_ONLY': '僅模擬',
    'CONDITIONAL_ESTIMATE': '通過條件的金額試算', 'AMOUNT_JPY': '以日圓金額換算', 'BOARD_LOT': '以整手股數計算',
    'TREND_PULLBACK': '趨勢回檔承接', 'RECLAIM_BREAKOUT': '站回／突破承接', 'MOMENTUM_CONTINUATION': '動能延續',
    'SUPPORT_PROBE': '支撐試單', 'researcher': '研究員', 'reviewer': '反證員', 'search': '尋標員',
    'COMPLETE': '完成', 'COMPLETED': '已完成', 'DEGRADED': '完成但有資料或研究缺口', 'FAILED': '失敗',
    'PARTIAL_FAILURE': '部分研究未完成', 'DATA_INCOMPLETE': '行情資料未齊全',
    'MODEL_RESEARCH_INCOMPLETE': '模型研究未齊全', 'LEDGER_ERRORS': '觀察紀錄寫入有缺口',
    'IDLE': '待命', 'RUNNING': '執行中', 'NOT_RUN': '未執行', 'UNKNOWN': '未知',
    'OBSERVATION': '觀察用資料', 'REJECTED': '已拒絕', 'raw_unverified': '原始價格，公司行動未核實',
    'CONDITIONAL_MANUAL_REVIEW': '條件式人工評估', 'PRICE_MOVE': '股價漲幅目標',
    'MODEL_EXTRACTED_NOT_INDEPENDENTLY_VERIFIED': '模型抽取，尚未獨立核實',
    'UNVALIDATED_RESEARCH_PRIORITY_NOT_PROBABILITY': '未驗證的研究排序，非上漲機率',
    'UNVALIDATED_HYPOTHESIS_NOT_WIN_PROBABILITY': '未驗證假說，非勝率',
    'RULE_CONFIRMED': '規則確認（暫定）', 'RETEST_HELD': '回測守住，待確認', 'BOUNCE_SEEN': '已反彈，待回測',
    'BASE_FORMING': '底部形成中', 'INVALIDATED': '支撐已失效', 'EXPIRED': '型態過期',
    'NO_SETUP': '尚無型態', 'INSUFFICIENT_DATA': '資料不足', 'NEAR_SUPPORT': '接近支撐',
    'EXTENDED': '價格已延伸，避免追高', 'EARNINGS': '財報', 'COMPANY_IR': '公司投資人關係資料',
    'SUPPORTS': '偏支持', 'CONTRADICTS': '偏反對', 'NEUTRAL': '中性', 'DATE_ONLY': '僅有日期',
}

for line in '''
ACTIVE_DUAL_REVIEW_INCOMPLETE|研究與反證尚未齊全
ACTIVE_PUBLIC_SOURCE_GAPS_REQUIRE_REVIEW|公開資訊仍有缺口，需核對
ACTIVE_SEARCH_SUBMISSION_REQUIRED|須先由尋標員提交
AGENT_WATCH_ONLY|Agent建議繼續觀察
ANCHOR|支撐錨點
ANY_LATER_DAILY_LOW_BELOW_FIXED_LOWER_REQUIRES_REJECTION|後續日線低點跌破固定下緣即否決
AWAITING_INDEPENDENT_AGENT_REVIEWS|等待研究與反證各自審查
BOUNCE|反彈證據
COMMON_STOCK_CLASSIFICATION_UNVERIFIED|普通股分類待核實
COMPANY_EVENTS_UNVERIFIED|公司公告與決算事件待核實
COMPANY_GUIDANCE_RAISE_IS_NOT_CONSENSUS_SURPRISE|公司上修不等於超過市場預期
COMPANY_RELEASE|公司公告
CONFIRMATION|確認證據
CONTEXT_GATE_BLOCKED|公司或市場背景條件未通過
CORPORATE_ACTIONS_UNVERIFIED|公司行動未核實
CORPORATE_ACTION_IN_WINDOW_REQUIRES_REVIEW|期間含公司行動，需核對價格
COUNTEREVIDENCE|反證資訊
CURRENT_RETRIEVAL_NOT_HISTORICAL_PIT|本次取得資料，非歷史當時快照
DAILY_BARS_CANNOT_CONFIRM_INTRADAY_TRIGGER_ORDER|日線無法確認盤中觸發先後
DAILY_CLOSE_BELOW_FROZEN_SUPPORT_LOWER|日收盤跌破固定支撐下緣才判此通道失效
DAILY_OHLC_DOES_NOT_PROVE_INTRADAY_ORDER|日線開高低收無法證明盤中先後
DATA_COLLECTION_DEGRADED|本輪資料取得不完整
DATA_REJECTED|資料未通過檢查
DECLINE|下跌證據
DECLINE_DRIVER_UNVERIFIED|下跌原因未核實
DELAYED_QUOTE_OR_CONDITIONAL_TRIGGER|延遲報價或條件式觸發價
DOSSIER_UNKNOWN_OR_STALE|公司與公告資料缺少或過期
DUAL_REVIEW_REQUIRED|需要研究與反證共同審查
EARLY_WATCH|早期觀察
EXCHANGE|交易所資料
EXPLORATORY_PARAMETERS_AUTHORED_AFTER_REFERENCE_CASE_NOT_VALIDATED|參數於參考案例後制定，尚未驗證
EXPLORATORY_SHORT_CYCLE_NOT_PREDICTED_WIN_RATE|探索性短線研究，非預測勝率
EXPLORATORY_SHORT_HOLD_NOT_ESTABLISHED_SUPPORT|短暫守住的探索，不代表支撐確立
EXTENDED_FROM_SUPPORT|目前離支撐過遠
FINANCIAL_DISCLOSURE_NOT_RETRIEVED|尚未取得財務公告
GUIDANCE|公司展望
H001_METRICS|H-001型態數值證據
H002_METRICS|H-002型態數值證據
HARD_INVALIDATION|固定失效條件成立
INDEPENDENT_OF_ATR_PERMISSION|此型態與ATR資格分開
INPUT_SOURCE_CLAIMS_REQUIRE_EXTERNAL_VERIFICATION|輸入的來源聲明仍需外部核實
INSUFFICIENT_SUPPORT_TESTS|支撐測試次數不足
INVALID_RISK_DISTANCE|風險距離無效
LATEST|最新完整日線
LATEST_COMPLETED_DAILY_CLOSE_NOT_LIVE_QUOTE|最近完整日線收盤，非即時價
LATEST_INTRADAY_UNDERCUT|最新日線低點穿過支撐下緣
LATEST_LOW_OUTSIDE_RETEST_BAND|最新低點不在回測帶內
LATEST_PREVIOUS_EPISODE_NOT_CURRENT_NEW_SUPPORT|最近前一段型態，非目前新支撐
LEDGER_INVALIDATED|追蹤帳本標記型態失效
MANUALLY_VERIFY_NEXT_SESSION_PRICE_AND_GAP|人工核對下一時段價格與跳空
MARKET_STRESS_UNKNOWN|市場壓力尚未分類
NET_RR_BELOW_MINIMUM|成本後風報比未達門檻
NEWS|新聞
NEW_HYPOTHESIS_DOES_NOT_REPAIR_PREVIOUS_FAILED_EPISODE|新假說不會修復先前失敗型態
NOT_AVAILABLE_NO_ACCOUNT_OR_POSITION_DATA|缺少完整帳戶資料，無法執行帳戶虧損保護
NOT_NEAR_SUPPORT|距支撐較遠
NOT_OUT_OF_SAMPLE_VALIDATED|尚未經樣本外驗證
NOT_SELECTED_BY_SEARCH|本輪尋標未提交，不能視為已研究否決
NO_ACTIVE_H002_SETUP|目前沒有有效H-002型態
NO_AVAILABLE_BARS|沒有可分析完整日線
NO_CURRENT_SETUP|目前沒有有效型態
NO_MATCH|尚未符合此型態
NO_OUT_OF_SAMPLE_VALIDATED_EDGE|尚無樣本外優勢證據
NO_VERIFIED_MARKET_CONSENSUS|沒有可核實的市場事前預期
NO_VERIFIED_POSITIVE_SURPRISE|沒有可核實的正向超預期證據
OBSERVATION_ONLY|僅供觀察
ONE_DAILY_HOLD_IS_WEAKER_THAN_MULTI_TEST_SUPPORT|單日守住的證據弱於多次支撐測試
PARAMETERS_NOT_CALIBRATED_ON_ADVANTEST|參數並非以Advantest校準
PATTERN_VOLUME_BASIS_UNVERIFIED|此型態量能口徑待核實
POINT_IN_TIME_NOT_VERIFIED|非經核實的歷史當時可得快照
POST_REFERENCE_CASE_EXPLORATORY_DEFINITION|參考案例後制定的探索規則
PRE_ANCHOR_HISTORICAL_HIGH|錨點之前的歷史高點
PRICE_DATA_REJECTED|價格資料未通過檢查
PUBLICATION_TIME_UNKNOWN|來源發布時間不明
PUBLICATION_TOO_OLD|來源發布時間過舊
PUBLIC_SEARCH_PARTIAL_OR_UNEXECUTED_ATTEMPT|公開搜尋僅部分完成或有未執行嘗試
RAW_VOLUME_COMPARABILITY_UNVERIFIED|原始成交量可比性未核實
RAW_VOLUME_MAY_BE_UNVERIFIED|原始量能口徑可能尚未核實
RECENT_SUPPORT_HOLD_NOT_CONFIRMED|近期支撐守住尚未確認
RECENT_VOLUME_CONTRACTION_NOT_OBSERVED|近期未觀察到縮量
REFERENCE_PLAN_UNAVAILABLE|尚無有效參考計畫
REGULATOR|監管機關資料
RESEARCH_NOT_APPROVED|研究資格未通過
RESEARCH_REJECTED|研究提出否決
RETEST|回測證據
SECTOR|產業資訊
SHORT_DIRECTION_UNCONFIRMED|短線方向尚未確認
SHORT_ENTRY_TYPE_NOT_FORMED|短線買入型態尚未形成
SHORT_MOMENTUM_READY|動能延續候選
SHORT_PULLBACK_READY|趨勢回檔候選
SHORT_RECLAIM_READY|已站回／突破候選
SHORT_RECLAIM_TRIGGER|站回／突破仍待觸發
SHORT_RELATIVE_VOLUME_TOO_LOW|短線相對成交量不足
SHORT_MOVE_OVERHEATED|短線漲幅過熱
SHORT_LIQUIDITY_TOO_LOW|短線流動性不足
SHORT_RECENT_RANGE_TOO_SMALL|近期振幅不足
SHORT_SIGNAL|短線訊號證據
SHORT_WATCH|短線繼續觀察
SNAPSHOT_ONLY_REVALIDATE_BEFORE_NEXT_SESSION|僅本輪快照有效，下次時段前需重驗
STABILITY_NOT_CONFIRMED|穩定條件未確認
SUPPORT_HELD|暫時守住支撐
SUPPORT_INVALIDATED|支撐已失效
SUPPORT_NOT_CONFIRMED|支撐尚未確認
SUPPORT_NOT_RULE_CONFIRMED|支撐未完成規則確認
SUPPORT_RECLAIMED|已站回支撐
SUPPORT_TESTS_NOT_INDEPENDENT_SAMPLES|支撐測試並非獨立統計樣本
TACTICAL_PRICE_STOP_REPRICED_BEFORE_ENTRY_FIXED_AFTER_CONFIRMED_FILL|進場前重算戰術風險線，確認成交後固定
TACTICAL_STOP_INSIDE_ATR_NORMAL_NOISE|戰術風險線落在ATR日常波動範圍內
TACTICAL_STOP_MAY_BE_HIT_BY_NORMAL_VOLATILITY|正常波動也可能觸及戰術風險線
TARGET_IS_OBJECTIVE_NOT_PRICE_FORECAST|目標是策略設定，不是價格預測
TARGET_NOT_ABOVE_REFERENCE|目標價不高於參考進場價
TECHNICAL_GATE_BLOCKED|技術資格條件未通過
TRADABILITY_UNVERIFIED|停牌／鎖價與可交易性未核實
TURNOVER_ESTIMATED_CLOSE_TIMES_VOLUME|成交值以收盤價乘股數估算
UNDERCUT_REVIEW|跌破後待核對
USER_MUST_CHOOSE_AFFORDABLE_RISK_AND_CONFIRM_FILL|使用者需自行選擇可承受風險並確認成交
VERIFY_CORPORATE_ACTIONS_AND_BROKER_ELIGIBILITY|核實公司行動與券商可交易資格
VOLATILE_WATCH|高波動觀察，方向未確認
VOLUME_BREAKOUT_NOT_CONFIRMED|放量突破尚未確認
VOLUME_CONTRACTION_NOT_CONFIRMED|縮量尚未確認
VOLUME_REVIEW|量能待核對
YAHOO_CHART_UNOFFICIAL_DAILY|Yahoo非官方日線來源
YAHOO_UNOFFICIAL_SOURCE|Yahoo非交易所官方行情來源
'''.strip().splitlines():
    key, label = line.split('|', 1)
    VALUES[key] = label


def code_label(value):
    return f'{VALUES[value]}（{value}）' if isinstance(value, str) and value in VALUES else str(value)


def _scalar(value, key):
    if value is None:
        return '未提供／不適用（null）'
    if type(value) is bool:
        return '是（true）' if value else '否（false）'
    if type(value) in (int, float):
        if key.endswith('_fraction'):
            return f'{value:.2%}（原值 {value:g}）'
        return f'{value:,.4f}'.rstrip('0').rstrip('.') if type(value) is float else f'{value:,}'
    return code_label(value)


def labeled_tree(value, key=''):
    """Display all original fields, including unknown future fields, with safe text."""
    if isinstance(value, dict):
        rows = []
        for k, v in value.items():
            label = LABELS.get(k, '新增／未登錄欄位')
            help_text = f'<small>{escape(HELP[k])}</small>' if k in HELP else ''
            content = labeled_tree(v,k)
            if isinstance(v,dict) and v:
                content=f'<details><summary>展開 {escape(label)}（{len(v)} 個欄位）</summary>{content}</details>'
            rows.append(f'<div class="field-row"><div><b>{escape(label)}</b> <code>{escape(k)}</code>{help_text}</div>'
                        f'<div>{content}</div></div>')
        return '<div class="field-tree">'+(''.join(rows) or '無項目')+'</div>'
    if isinstance(value, list):
        if not value:return '無項目（空清單）'
        if all(not isinstance(v,(dict,list)) for v in value):
            return '<ul>'+''.join(f'<li>{escape(_scalar(v,key))}</li>' for v in value)+'</ul>'
        return ''.join(f'<details><summary>第 {i+1} 項</summary>{labeled_tree(v,key)}</details>' for i,v in enumerate(value))
    return escape(_scalar(value,key))


def render_stock_fields(item):
    display={k:item[k] for k in ('symbol','name','opportunity_decision','execution_plan','market_quote','agent_reviews','manual_action') if k in item}
    display.update(item)
    return labeled_tree(display)+f'<details><summary>原始JSON（完整保留）</summary><pre>{escape(json.dumps(item,ensure_ascii=False,indent=2))}</pre></details>'


def render_field_guide(report):
    cards = []
    for item in report.get('securities',[]):
        symbol = str(item['symbol'])
        cards.append(f'<details id="json-{escape(symbol,quote=True)}"><summary>{escape(symbol)} {escape(item.get("name",""))}：中文欄位與原始JSON</summary>'
                     f'{render_stock_fields(item)}</details>')
    return '''<style>
#field-guide{overflow-wrap:anywhere;background:white;padding:20px;border:1px solid #dde4eb;border-radius:8px}
#field-guide>.field-stock, #field-guide>details{margin:12px 0;padding:10px;border:1px solid #dbe3ea}
#field-guide summary{cursor:pointer;font-weight:600;padding:6px}#field-guide pre{white-space:pre-wrap}
.field-row{display:grid;grid-template-columns:minmax(180px,1fr) minmax(0,2fr);gap:12px;padding:8px;border-bottom:1px solid #e4e9ed}
.field-row small{display:block;color:#536579}.field-tree .field-tree{border-left:2px solid #dbe3ea;padding-left:8px}
@media(max-width:800px){.field-row{grid-template-columns:1fr}.field-tree .field-tree{padding-left:3px}}
</style><section id="field-guide"><h2>個股JSON中文欄位說明</h2>
<p>建議依序看「積極人工機會評估 → Python部位與人工執行試算 → 延遲參考報價 → 兩方評語」。每個欄位保留原始鍵名，方便對照JSON；僅改顯示，不改Agent輸入。</p>
<p>比例0.02顯示為2%；RR是報酬／風險倍數，分數不是勝率。空值表示未提供或不適用，不能當成0。不同型態的支撐與風險線分開看。</p>
'''+''.join(cards)+'''</section><script>
function openFieldHash(){try{const target=document.getElementById(decodeURIComponent(location.hash.slice(1)));if(target&&target.tagName==='DETAILS')target.open=true;}catch(e){}}
window.addEventListener('hashchange',openFieldHash);openFieldHash();
</script>'''
