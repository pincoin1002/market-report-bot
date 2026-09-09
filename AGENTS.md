# market-report-bot — AGENTS.md

放到 repo root（GitHub：`pincoin1002/market-report-bot`，public）

---

## 專案是什麼

自動生成台股／美股每日報告，推送到 **Telegram + Email**。
硬性需求：**全雲端跑，不能依賴我的電腦開機**。

## 架構

- 產文：**Gemini API**（免費層，key 從 aistudio.google.com 拿）
- 排程與執行：**GitHub Actions**（免費、雲端）
- 推送：Telegram Bot + Gmail SMTP

四個 workflow：`tw-open`、`tw-close`、`us-open`、`us-close`

## 目錄

```
.github/        workflows
prompts/        報告 prompt
scripts/        Python
reports/        輸出
requirements.txt
README.md
```

## GitHub Secrets（已設定，共 6 個）

| Name | 內容 |
|---|---|
| `GEMINI_API_KEY` | Gemini API key |
| `TELEGRAM_BOT_TOKEN` | BotFather 給的 token |
| `TELEGRAM_CHAT_ID` | 我的 chat id |
| `EMAIL_USERNAME` | 寄件 Gmail |
| `EMAIL_PASSWORD` | Gmail **App Password**（16 碼，非登入密碼） |
| `EMAIL_TO` | 收件 Email |

⚠️ **Telegram bot token 曾貼在對話中 → 去 BotFather 用 `/revoke` 重發一把新的，更新 Secret。**

## 現況

檔案已 push、Secrets 已填齊，但 **workflow 從未確認跑成功過**。

## 接手第一件事

1. 到 Actions 頁手動 `Run workflow` 跑 `us-close`
2. 看 log 定位失敗點（常見：Gemini quota、SMTP 認證、Telegram chat id）
3. 修到四個 workflow 都能手動綠燈，再確認 cron 時區設定對不對（台灣 UTC+8，GitHub Actions 用 UTC）

## 報告內容偏好

我自己看盤用 財報狗、Goodinfo、籌碼K線、玩股網，聽 股癌。報告要**具體、有數字**，不要通用市場評論。
