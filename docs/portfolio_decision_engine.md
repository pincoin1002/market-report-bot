# 私人逐檔建議的證據與規則

PIOS 是唯一持倉與成本來源。Engine 唯讀，不下單、不改 quantity。
每個有效 position_id 產出一筆 PortfolioDecision；缺行情也保留「觀察」。

## 占比與配置

單檔市值 = canonical quantity × 該檔有效報價。
台幣市值使用同一份有效 USD/TWD；總值是全部有效持倉的台幣市值合計。
只有所有持倉與必要匯率均可驗證時才顯示占比；缺一檔就不以部分總值冒充全組合。
這是「最新可得報價的持倉估值占比」，不是同步即時 NAV，未含現金、負債，
也不產生混合交易時窗的組合報酬或相對績效。

優先使用 PIOS 明示且 allocation_quality=VERIFIED 的單檔 max_weight/target_weight，
或 allocation_rules.quality=VERIFIED 的類別上限。否則使用公開 TOML 的透明檢視上限。
已驗證的根層 target_weight_by_instrument / max_weight_by_instrument 也會讀入，並保留原始 quantity。
預設上限不是已確認的個人目標；沒有已驗證 target_weight 不會僅因低於上限而建議加碼。
預設值為股票20%、廣泛ETF40%、主題ETF10%、加密幣10%、代幣2%、穩定幣20%。
這些是可調整的產品風險規則，非歷史績效最佳化或模型推測。

## 決策順序

1. 可驗證且仍在近期時窗的事件，配合來源連結一致的私人研究證據，明確使原假設失效，或 PIOS 已確認配置目標為零：退出。
2. 全部持倉估值可驗證且單檔超過適用上限、超過已確認目標加上容忍帶，或核實假設轉弱／估值過高：減碼。
3. 低於已驗證配置目標、最新事件檢查有效，且資產專用的正面條件成立：加碼。
4. ETF 的基金角色已核對且未觸及上限，或穩定幣報價接近錨定值並保留結算角色，
   或來源支持股票假設完整且配置適當：持有。事件或成本缺漏降低信心。
5. 關鍵證據未齊或互相衝突：觀察，明確寫出待核對項目；目前維持部位、不追加曝險。

價格漲跌本身只能形成波動訊號，不能單獨造成加碼、減碼或退出。
加碼的正面條件：股票需已驗證假設、營運及估值；ETF 需基金角色已核對且沒有未處理的同類基金重疊；
加密資產需來源支持的協議／使用假設改善與流動性核實；穩定幣需已確認結算用途、流動性／兌回渠道及未見錨定偏離。
不要求 ETF、crypto、stablecoin 虛構公司財報。目標調整容忍帶預設為一個百分點，可在 TOML 修改。
ETF 重疊採已查核的基金角色／指數範圍；未讀最新成分股時不宣稱精確重疊率。
所有規則保留 rule_id、quote_id、研究來源、event 時效與資料缺口。

## 私人研究輸入

可選 PORTFOLIO_DECISION_EVIDENCE_PATH 指向私人 JSON 陣列，逐筆使用 HoldingDecisionEvidence schema。
verified=true、verified_at 在七日內及 source_ids 非空才可採用。
event_effect 必須連結到同一 source_url、原始發布日期已核實且48小時內的 material event。
不得把 Gemini 的買賣投票或 HIGH severity 自動等同原投資假設失效。
未提供研究輸入時，估值、營運與原投資假設保持未知。

## 成本與 sizing

cost_basis 是每單位平均成本。只有 PIOS basis_quality/cost_basis_status=VERIFIED，
或成本物件 status/quality=VERIFIED 且提供 average_cost/unit_cost，並與部位幣別一致時，
才顯示成本及 (現價/成本−1) 的未實現損益百分比。不從價格、交易紀錄或舊估值猜成本。
缺成本不刪除方向建議。未核實可動用現金、配置目標及 sizing 方法前，不生成精確交易數量。
此版本只輸出方向，trade_quantity 永遠空值；沒有交易 API。

## 私人邊界與 Telegram

JSON、全文與分段存於 gitignored runtime data；GitHub uploader 只接收 Fernet 加密的 decision JSON。
PUBLIC report renderer 不接收 decision brief。Telegram 以完整持股段落與資產群組分段，
每段使用 UTF-16 長度上限並保留全部持股；摘要與優先處理放在第一段。
