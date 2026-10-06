# CTEE 盤前新聞 AI 摘要

每個交易日早上自動抓取工商時報盤前新聞 → Groq AI 摘要 → Email 寄送。

## 流程

| 階段 | 檔案 | 工作 | 輸出 |
|---|---|---|---|
| 1 | `stage1_scrape.py` | 找今天的盤前 Hub、抓各篇全文 | `data/premarket_result.json` |
| 2 | `stage2_ai.py` | Groq 摘要（不開瀏覽器） | `data/premarket_ai_result.json` |
| 3 | `stage3_notify.py` | 寄送摘要，或寄送異常／無資料通知 | Email |

每一階段無論成功或失敗都會寫出含 `status` 與 `reason` 的 JSON，第三階段依此決定寄哪種信。

## GitHub 設定

**Settings → Secrets and variables → Actions**

Secrets（必要）：

- `GROQ_API_KEY`：Groq API Key
- `MAIL_FROM`：寄件 Gmail
- `MAIL_PASSWORD`：Gmail 應用程式密碼（需先開啟兩步驟驗證，不是登入密碼）
- `MAIL_TO`：收件者，多人用逗號分隔

Variables（選用）：

- `GROQ_MODEL`：模型名稱，未設定時使用程式內預設值

建議使用 **private repo**：artifact 內含文章全文。

## 第一次執行

到 Actions 頁面選擇此 workflow → **Run workflow** 手動執行，確認：

1. log 中文章內文來源顯示 `json-ld` 或 `dom:...`，字數合理
2. 下載 artifact 檢查 `data/` 內容；若被擋，`debug/` 會有截圖與 HTML

## 可調整的環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `HUB_WAIT_ATTEMPTS` | 3 | 今天的盤前尚未發布時，檢查次數 |
| `HUB_WAIT_MINUTES` | 10 | 每次檢查間隔（分鐘） |
| `MAX_LOAD_MORE` | 5 | 最多點擊「載入更多」次數 |
| `MAX_CONTENT_CHARS` | 4000 | 送給 AI 的內文上限 |
| `AI_REQUEST_DELAY` | 3 | AI 請求間隔（秒） |

## 改用自架 runner（大幅降低被擋機率）

Settings → Actions → Runners → New self-hosted runner，依指示在家中常開的電腦安裝，
然後把 workflow 的 `runs-on: ubuntu-latest` 改成 `runs-on: self-hosted`。
macOS 上 `playwright install --with-deps` 的系統套件步驟可省略 `--with-deps`。

## 本機執行

```bash
pip install -r requirements.txt
python -m playwright install chromium
export GROQ_API_KEY=... MAIL_FROM=... MAIL_PASSWORD=... MAIL_TO=...
python stage1_scrape.py && python stage2_ai.py && python stage3_notify.py
```

在 Jupyter 中請用 `!python stage1_scrape.py` 執行。
