# TWST JP Event Notifier

Twisted Wonderland JP 服活動行程自動整理工具。  
本專案會定期從 Kamigame 的 TWST 活動頁抓取活動、抽卡與歷年活動資料，產生可訂閱的 iCalendar (`.ics`) 日曆，並在偵測到新情報時推送到 Discord。

## 功能

- 自動抓取 TWST JP 服活動與抽卡資訊
- 解析 Kamigame 2020-2026 歷年活動年份分頁，避免舊活動被放到錯誤年份
- 產生 `twst_events.ics`，可用 Google Calendar、Apple Calendar 等日曆服務訂閱
- 使用 `state.json` 記錄已處理活動，避免重複推播
- 透過 GitHub Actions 每小時自動更新
- 可選擇設定 Discord Webhook，收到新活動通知

## 日曆訂閱

Google Calendar 可以用下列 ICS URL 訂閱：

```text
https://raw.githubusercontent.com/shizuku-kuroba/TWST-Event-Notifier/main/twst_events.ics
```

Google Calendar 訂閱方式：

1. 開啟 Google Calendar
2. 在左側「其他日曆」旁點選 `+`
3. 選擇「透過網址」
4. 貼上上方 ICS URL
5. 點選「新增日曆」

注意：Google Calendar 對外部 ICS 的同步可能延遲數小時到一天。如果想立即看到更新，可以移除後重新訂閱。

## Discord 通知設定

如果要讓 GitHub Actions 推送 Discord 通知，需要在 GitHub repo 設定 Secret：

```text
TWST_DISCORD_WEBHOOK_URL
```

設定位置：

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

Secret 的值請填入 Discord channel 的 Webhook URL。

如果沒有設定此 Secret，程式不會推送 Discord；尚未結束的新活動也不會被標記為已通知，避免之後漏推。

## 自動更新

GitHub Actions workflow 位於：

```text
.github/workflows/twst_notify.yml
```

目前設定：

- 每小時自動執行一次
- 也可以在 GitHub Actions 頁面手動執行 `Run workflow`
- 執行後會自動提交更新後的 `state.json` 與 `twst_events.ics`

## 本機執行

安裝依賴：

```bash
pip install -r requirements.txt
```

執行：

```bash
python twst_kamigame_notifier.py
```

如需本機測試 Discord 推播，可先設定環境變數：

```bash
export TWST_DISCORD_WEBHOOK_URL="你的 Discord Webhook URL"
python twst_kamigame_notifier.py
```

Windows PowerShell：

```powershell
$env:TWST_DISCORD_WEBHOOK_URL="你的 Discord Webhook URL"
python twst_kamigame_notifier.py
```

## 主要檔案

- `twst_kamigame_notifier.py`：主程式，負責抓取、解析、推播與產生日曆
- `twst_events.ics`：可訂閱的 iCalendar 檔案
- `state.json`：已處理活動狀態，避免重複通知
- `requirements.txt`：Python 依賴套件
- `.github/workflows/twst_notify.yml`：GitHub Actions 自動執行設定

## 資料來源

活動資料來自 Kamigame：

```text
https://kamigame.jp/ツイステ/イベント/index.html
```

本專案只是整理公開網頁資訊並產生個人用日曆/通知，不隸屬於 Disney、Aniplex、f4samurai 或 Kamigame。

## 注意事項

- 網頁結構變動時，解析邏輯可能需要更新
- Google Calendar 訂閱外部 ICS 不會即時刷新
- Discord Webhook URL 請勿提交到 repo，請使用 GitHub Secrets
