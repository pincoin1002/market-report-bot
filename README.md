# 市場報告自動化系統

每天自動生成 4 份繁體中文 institutional-grade 市場報告，推送到 Telegram 和 Email。完全運行於 GitHub Actions，不需要本機電腦開著。

## 報告一覽

| 報告名稱 | 觸發時間（台北） | 觸發日 | Workflow 檔案 |
|---------|--------------|-------|--------------|
| 台股開盤戰報 | 07:50 | 週一～週五 | `tw-open.yml` |
| 美股收盤日報 | 08:30 | 週二～週六 | `us-close.yml` |
| 台股收盤日報 | 14:30 | 週一～週五 | `tw-close.yml` |
| 美股開盤日報 | 21:00 / 22:00（依美東夏冬令自動擇一） | 週一～週五 | `us-open.yml` |

---

## 快速開始

### Step 1 — 取得 Google Gemini API Key

1. 前往 [aistudio.google.com](https://aistudio.google.com/)
2. 登入 Google 帳號
3. 點選左側 **Get API key** → **建立 API 金鑰**
4. 選擇專案（或建立新專案），複製產生的 key（格式：`AIzaSy...`）
5. 妥善保存

> **免費額度**：`gemini-2.0-flash` 每天 **1,500 requests 完全免費**，**不需要綁信用卡**。4 份/天 × 30 天 = 120 requests/月，遠低於免費上限，零成本運行。

---

### Step 2 — 建立 Telegram Bot（可選）

**取得 Bot Token：**
1. 在 Telegram 搜尋 `@BotFather`
2. 發送 `/newbot`
3. 輸入 Bot 名稱（顯示名，例如：`我的市場報告`）
4. 輸入 Bot 帳號（必須 `_bot` 結尾，例如：`mymarket_bot`）
5. 複製 BotFather 給你的 Token（格式：`1234567890:ABCdef...`）

**取得 Chat ID（推送到個人帳號）：**
1. 在 Telegram 搜尋 `@userinfobot`
2. 發送任意訊息，它會回覆你的 User ID（即 Chat ID）

**取得 Chat ID（推送到群組）：**
1. 把你的 Bot 加入群組並給予管理員權限
2. 在群組發送任意訊息
3. 開啟瀏覽器訪問：`https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. 在 JSON 回應中找 `"chat":{"id":-100XXXXXXXXXX}`（群組 ID 是負數）

---

### Step 3 — 取得 Gmail App Password（可選）

> 需要先開啟 Google 帳號的兩步驟驗證

1. 前往 Google 帳號設定 → **安全性**
2. 找到「兩步驟驗證」→ 滑到底部 → **應用程式密碼**
3. 選擇「郵件」和「其他裝置」，輸入名稱（如 `GitHub Market Report`）
4. 複製 16 位數 App Password（格式：`xxxx xxxx xxxx xxxx`，去掉空格後填入）

> 如使用其他郵件服務：修改 `EMAIL_SMTP_SERVER`（如 `smtp.qq.com`）和 `EMAIL_SMTP_PORT`

---

### Step 4 — 建立 GitHub Repo 並推送程式碼

```bash
# 建立本機 git repo
cd /path/to/this/project
git init
git add .
git commit -m "Initial commit: market report automation"

# 在 GitHub 建立新 repo（不要初始化 README）
# 前往 github.com → New repository → 複製 repo URL

# 推送
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git branch -M main
git push -u origin main
```

---

### Step 5 — 設定 GitHub Secrets

前往你的 GitHub Repo → **Settings** → **Secrets and variables** → **Actions**

**必填 Secrets（Secrets 頁籤）：**

| Secret 名稱 | 值 | 備註 |
|------------|---|------|
| `GEMINI_API_KEY` | `AIzaSy...` | 必填，沒有此 key 程式無法運行 |

**可選 Secrets（不填則跳過該推送管道）：**

| Secret 名稱 | 值 | 備註 |
|------------|---|------|
| `TELEGRAM_BOT_TOKEN` | `1234567890:ABCdef...` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | `123456789` 或 `-100123456` | 個人 ID 或群組 ID |
| `EMAIL_SMTP_SERVER` | `smtp.gmail.com` | Gmail SMTP |
| `EMAIL_SMTP_PORT` | `587` | TLS port，Gmail 用 587 |
| `EMAIL_USERNAME` | `yourname@gmail.com` | 寄件者 Gmail |
| `EMAIL_PASSWORD` | `xxxxxxxxxxxxxxxx` | App Password（16位，無空格） |
| `EMAIL_TO` | `a@gmail.com,b@gmail.com` | 收件人，逗號分隔多個地址 |

**可選 Variables（Variables 頁籤）：**

| Variable 名稱 | 值 | 備註 |
|--------------|---|------|
| `REPORT_MODEL` | `gemini-2.0-flash` | 留空預設用 gemini-2.0-flash |

---

### Step 6 — 手動觸發測試

設定完成後，先手動測試確認一切正常：

1. 前往 GitHub Repo → **Actions** 頁籤
2. 左側選擇你要測試的 Workflow（例如「台股開盤戰報」）
3. 點選右側 **Run workflow** 按鈕
4. 可選擇 **dry_run: true**（只生成報告，不發 Telegram / Email）進行初步測試
5. 點選 **Run workflow** 確認
6. 等待約 2～5 分鐘，workflow 完成後點進去查看 log
7. 確認 log 顯示 `done` 表示成功
8. 在 workflow run 頁面底部的 **Artifacts** 區域下載報告 .md 檔確認內容

---

### Step 7 — 修改報告時間

修改 `.github/workflows/` 裡的 yml 檔案中的 `cron` 表達式。

**Cron 格式說明：**
```
分鐘(0-59)  小時(0-23)  日(1-31)  月(1-12)  星期(0-7, 0和7都是週日)
```

**時區換算（台北 → UTC）：**
- 台北時間 = UTC + 8
- 因此 UTC 時間 = 台北時間 - 8 小時

**範例：想改成台北時間 08:00 週一到週五觸發：**
```yaml
- cron: "0 0 * * 1-5"   # UTC 00:00 = 台北 08:00
```

**當前設定的 Cron 對照表：**

| 報告 | Cron（UTC） | 台北時間 | 說明 |
|-----|-----------|--------|------|
| 台股開盤 | `50 23 * * 0-4` | 07:50（週一～五） | UTC 前一天 23:50 |
| 美股收盤 | `30 0 * * 2-6` | 08:30（週二～六） | UTC 00:30 |
| 台股收盤 | `30 6 * * 1-5` | 14:30（週一～五） | UTC 06:30 |
| 美股開盤 | `0 13 * * 1-5` + `0 14 * * 1-5` | 21:00 / 22:00（週一～五） | runtime guard 只保留正確 09:00 ET |

---

### Step 8 — 夏令 / 冬令時間

美股開盤日報使用雙 cron：

- `0 13 * * 1-5`
- `0 14 * * 1-5`

程式用 `America/New_York` runtime guard 判斷哪一次是 09:00 ET，因此不需要手動切換夏令 / 冬令。非正確時段的重複 run 會自動 skipped。

---

## V2 資料完整性與持股監控

- 報價來源是 deterministic `QuoteObservation`，包含 ticker identity、session、market date、provider timestamp、quote id、quality status。
- Google Search 只用於新聞與事件脈絡，不是價格來源。
- 公開報告會先產生 structured draft，通過 validation 後才送 Telegram / Email。
- 私人持股輸出是 `PortfolioActionBrief`，狀態只有 `NO_MATERIAL_CHANGE` / `WATCH` / `ACTION_REVIEW` / `DATA_BLOCKED`。
- daily bot 不會自動產生 BUY / SELL / ADD / TRIM 股數；預設 `SIZE_NOT_COMPUTED`。
- 若持股報價 coverage 不到 100%，或 quote 是 stale / suspect / conflicting，私人 Action Brief 會 fail closed，不送錯誤建議。
- `portfolio.json` plaintext、`data/*.json` runtime artifacts 仍不進 Git；`portfolio.json.enc` 繼續搭配 `PORTFOLIO_KEY` 使用。

---

### Step 9 — 常見錯誤排查

**❌ Error: GEMINI_API_KEY is not set**
- 確認 GitHub Secrets 中有設定 `GEMINI_API_KEY`
- Secret 名稱必須完全一致（大小寫）

**❌ google.genai.errors.APIError / PermissionDenied**
- API Key 格式不正確或已過期
- 前往 aistudio.google.com 確認 key 是否有效，並確認已啟用 Generative Language API

**❌ Telegram 無法收到訊息**
- 確認 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 都已設定
- 先對你的 Bot 發送一條訊息（群組需先邀請 Bot）
- 確認 Bot 有發送訊息的權限

**❌ Email 發送失敗（Gmail）**
- 確認使用 App Password，不是 Gmail 登入密碼
- 確認已開啟兩步驟驗證
- 確認 `EMAIL_SMTP_PORT` 為 `587`（TLS），不是 465（SSL）

**❌ Workflow 沒有自動觸發**
- GitHub Actions 的 cron 排程有時會延遲 10～15 分鐘
- 確認 `.github/workflows/` 目錄和 yml 檔已成功推送到 `main` 分支
- 前往 Actions 頁面確認 Workflow 是否被 Disabled

**❌ 報告內容不完整或缺少數據**
- 這是正常現象，特定數據在特定時段可能無法搜尋到
- 報告中會標示「暫無可靠數據」而非編造數據
- 可調整 `max_tokens`（在 generate_report.py 第 56 行）增加到 10000

**❌ TimeoutError 或 Rate Limit**
- 增加 workflow 的 `timeout-minutes`
- Gemini 免費版有速率限制（RPM 上限），多次失敗可在 workflow 加入 retry 邏輯，或升級 Google AI Studio 付費方案

---

## 專案結構

```
.
├── .github/
│   └── workflows/
│       ├── tw-open.yml      # 台股開盤戰報（週一～五 07:50）
│       ├── us-close.yml     # 美股收盤日報（週二～六 08:30）
│       ├── tw-close.yml     # 台股收盤日報（週一～五 14:30）
│       └── us-open.yml      # 美股開盤日報（週一～五 21:00）
├── prompts/
│   ├── tw_open.md           # 台股開盤報告提示詞
│   ├── us_close.md          # 美股收盤報告提示詞
│   ├── tw_close.md          # 台股收盤報告提示詞
│   └── us_open.md           # 美股開盤報告提示詞
├── scripts/
│   └── generate_report.py   # 主程式
├── reports/                 # 生成的報告（gitignored，上傳為 artifact）
├── requirements.txt
├── .gitignore
└── README.md
```

---

## 本機測試（可選）

```bash
# 建立虛擬環境
python3.12 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 安裝依賴
pip install -r requirements.txt

# 設定環境變數
export GEMINI_API_KEY="AIzaSy..."
export TELEGRAM_BOT_TOKEN="..."  # 可選
export TELEGRAM_CHAT_ID="..."    # 可選

# 生成報告（以台股開盤為例）
python scripts/generate_report.py tw_open

# 其他報告類型
python scripts/generate_report.py us_close
python scripts/generate_report.py tw_close
python scripts/generate_report.py us_open
```

---

## 自訂報告內容

修改 `prompts/` 目錄下的 `.md` 檔案即可自訂報告結構。

- `{{TODAY_DATE}}` 會在運行時自動替換為當天日期（台北時間，格式 `YYYY-MM-DD`）
- `{{TODAY_WEEKDAY}}` 會替換為中文星期（如「週三」）
- 可以新增或刪除章節
- 可以調整搜尋任務清單

---

## 技術細節

- **模型**：`gemini-2.0-flash`（可透過 `REPORT_MODEL` Variable 更改）
- **Web Search**：使用 Google Search Grounding（`types.Tool(google_search=types.GoogleSearch())`）
- **最大輸出**：8,000 tokens（約 4,000～6,000 繁體中文字）
- **Telegram**：自動分割超過 4,096 字元的訊息
- **Email**：HTML 格式，含自動 Markdown → HTML 轉換
- **報告存檔**：上傳為 GitHub Artifact，保留 90 天
