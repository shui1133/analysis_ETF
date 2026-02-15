<<<<<<< HEAD
# 台灣 ETF 投資分析系統

完整的 ETF 投資回測分析系統，支援多種投資組合策略。

## 🚀 快速部署到 Render

### 方法一：使用 render.yaml（推薦）

1. **將專案推送到 GitHub**
   ```bash
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin <你的 GitHub repo URL>
   git push -u origin main
   ```

2. **連接 Render**
   - 前往 [Render Dashboard](https://dashboard.render.com/)
   - 點擊 "New +" → "Blueprint"
   - 連接你的 GitHub repository
   - Render 會自動讀取 `render.yaml` 並部署

3. **等待部署完成**
   - 部署通常需要 3-5 分鐘
   - 完成後會提供一個 `.onrender.com` 網址

### 方法二：手動設定

1. **推送到 GitHub**（同上）

2. **在 Render 建立 Web Service**
   - 前往 [Render Dashboard](https://dashboard.render.com/)
   - 點擊 "New +" → "Web Service"
   - 連接你的 GitHub repository

3. **設定部署參數**
   ```
   Name: taiwan-etf-analyzer（或你想要的名稱）
   Region: Singapore（離台灣最近）
   Branch: main
   Runtime: Python 3
   Build Command: pip install -r requirements.txt
   Start Command: gunicorn app:app
   Instance Type: Free
   ```

4. **新增環境變數**（可選）
   - 在 "Environment" 頁籤加入：
   - `PYTHON_VERSION` = `3.11.0`
   - `RENDER` = `true`

5. **點擊 "Create Web Service"**

## 📁 專案結構

```
taiwan-etf-analyzer/
├── app.py                 # Flask 主程式
├── data_fetcher.py        # ETF 資料爬取模組
├── backtest.py            # 回測分析模組
├── requirements.txt       # Python 依賴
├── Procfile              # Render 啟動設定
├── render.yaml           # Render 部署設定
├── .gitignore            # Git 忽略檔案
├── templates/            # HTML 模板
│   └── index.html
├── test_crawlers.py      # 測試腳本（開發用）
└── test_system.py        # 測試腳本（開發用）
```

## ⚠️ 重要注意事項

### 1. 資料持久化問題
**問題**：Render 的免費方案使用暫存檔案系統（/tmp），每次重啟會清空資料。

**解決方案**：
- **短期**：每次使用前重新爬取資料（當前實作）
- **長期**：整合資料庫（PostgreSQL 或 MongoDB）儲存爬取的資料

### 2. 爬蟲限制
- yfinance 是最穩定的資料來源
- MoneyDJ 和 Goodinfo 可能有反爬蟲機制
- 建議在非高峰時段爬取資料

### 3. 免費方案限制
- Render 免費方案在 15 分鐘無活動後會休眠
- 首次訪問需要等待 30-60 秒喚醒
- 每月有 750 小時的免費額度

### 4. 效能優化建議
- 考慮加入 Redis 快取爬取的資料
- 定期預先爬取資料（透過 Render Cron Jobs）
- 使用 CDN 加速靜態資源

## 🔧 本地開發

```bash
# 1. 安裝依賴
pip install -r requirements.txt

# 2. 執行應用
python app.py

# 3. 開啟瀏覽器
# http://127.0.0.1:5000
```

## 🧪 測試功能

```bash
# 測試爬蟲功能
python test_crawlers.py

# 測試完整系統
python test_system.py
```

## 📊 支援的投資組合

### 保守型投資者
- 00878 (國泰永續高股息) 40%
- 00713 (元大台灣高息低波) 30%
- 00679B (元大美債20年) 30%

### 穩健型投資者
- 00919 (群益台灣精選高息) 35%
- 00929 (復華台灣科技優息) 40%
- 0056 (元大高股息) 25%

### 積極型投資者
- 006208 (富邦台50) 30%
- 00929 (復華台灣科技優息) 50%
- 00915 (凱基優選高股息30) 20%

## 🔐 環境變數說明

| 變數名稱 | 說明 | 預設值 |
|---------|------|--------|
| PORT | 應用程式監聽埠號 | 5000 |
| RENDER | 標示是否在 Render 環境 | - |
| PYTHON_VERSION | Python 版本 | 3.11.0 |

## 📝 授權

本專案僅供教育與研究用途。投資有風險，請謹慎評估。

## 🐛 已知問題與解決方案

### 問題：資料爬取失敗
**原因**：網站反爬蟲或網路問題  
**解決**：系統會自動使用模擬資料，但建議多嘗試幾次

### 問題：首次訪問很慢
**原因**：Render 免費方案休眠機制  
**解決**：等待 30-60 秒讓服務喚醒

### 問題：資料重啟後消失
**原因**：/tmp 目錄是暫存的  
**解決**：重新爬取資料，或升級到付費方案使用持久化儲存

## 🚀 未來改進計畫

- [ ] 整合 PostgreSQL 資料庫
- [ ] 加入 Redis 快取層
- [ ] 定時自動爬取資料
- [ ] 支援更多 ETF
- [ ] 加入技術分析指標
- [ ] 使用者帳號系統
- [ ] 投資組合比較功能

## 📞 問題回報

如有問題或建議，歡迎開 Issue 討論。
=======
# analysis_ETF
>>>>>>> 931bb19cc3de11cab7fde42b99600df6b130f315
