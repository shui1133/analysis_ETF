# 🚀 Render 部署檢查清單

## ✅ 部署前準備

- [ ] 確認所有檔案都已準備好
  - [ ] app.py
  - [ ] data_fetcher.py
  - [ ] backtest.py
  - [ ] requirements.txt
  - [ ] Procfile
  - [ ] render.yaml
  - [ ] runtime.txt
  - [ ] templates/index.html
  - [ ] README.md
  - [ ] .gitignore

- [ ] 確認 Git 已初始化
  ```bash
  git init
  git add .
  git commit -m "Initial commit for Render deployment"
  ```

- [ ] 建立 GitHub repository 並推送
  ```bash
  # 在 GitHub 建立新的 repository
  git remote add origin https://github.com/你的用戶名/你的repo名稱.git
  git branch -M main
  git push -u origin main
  ```

## 🔧 Render 設定步驟

### 方法 A：使用 Blueprint（自動部署，推薦）

1. - [ ] 登入 [Render](https://dashboard.render.com/)
2. - [ ] 點擊 "New +" → "Blueprint"
3. - [ ] 選擇 "Connect a repository"
4. - [ ] 授權 GitHub 並選擇你的 repository
5. - [ ] Render 會自動讀取 `render.yaml`
6. - [ ] 點擊 "Apply" 開始部署
7. - [ ] 等待 3-5 分鐘部署完成

### 方法 B：手動設定

1. - [ ] 登入 [Render](https://dashboard.render.com/)
2. - [ ] 點擊 "New +" → "Web Service"
3. - [ ] 連接 GitHub repository
4. - [ ] 填寫設定：
   ```
   Name: taiwan-etf-analyzer
   Region: Singapore
   Branch: main
   Runtime: Python 3
   Build Command: pip install -r requirements.txt
   Start Command: gunicorn app:app
   Instance Type: Free
   ```
5. - [ ] 新增環境變數（Advanced 頁籤）：
   - `PYTHON_VERSION` = `3.11.0`
   - `RENDER` = `true`
6. - [ ] 點擊 "Create Web Service"
7. - [ ] 等待部署完成

## 🧪 部署後測試

- [ ] 打開 Render 提供的 URL（格式：`https://你的應用名稱.onrender.com`）
- [ ] 等待 30-60 秒讓服務喚醒（首次訪問）
- [ ] 確認首頁正常顯示
- [ ] 測試爬取資料功能
  - [ ] 選擇一個投資組合類型
  - [ ] 點擊「開始爬取資料」
  - [ ] 等待爬取完成（可能需要 1-2 分鐘）
- [ ] 測試回測分析功能
  - [ ] 輸入投資參數
  - [ ] 點擊「執行回測分析」
  - [ ] 檢查結果圖表是否正常顯示
- [ ] 測試下載 CSV 功能

## ⚠️ 常見問題排查

### 部署失敗

**問題**：Build 失敗
- [ ] 檢查 `requirements.txt` 格式是否正確
- [ ] 檢查 Python 版本是否支援所有套件
- [ ] 查看 Render 的 Build Logs 找出具體錯誤

**問題**：啟動失敗
- [ ] 檢查 `Procfile` 內容是否正確
- [ ] 確認 `app.py` 中的啟動設定正確
- [ ] 查看 Render 的 Deploy Logs

### 運行問題

**問題**：頁面無法開啟
- [ ] 確認服務狀態是 "Live"
- [ ] 等待 30-60 秒（免費方案休眠喚醒時間）
- [ ] 檢查 Logs 是否有錯誤訊息

**問題**：爬取資料失敗
- [ ] 檢查網路連線
- [ ] yfinance 可能暫時無法使用，稍後再試
- [ ] 系統會自動使用模擬資料作為備案

**問題**：資料重啟後消失
- [ ] 這是正常的（免費方案限制）
- [ ] 需要重新爬取資料
- [ ] 考慮升級到付費方案或整合資料庫

## 📊 效能監控

部署後建議監控：
- [ ] CPU 使用率（Render Dashboard）
- [ ] 記憶體使用率
- [ ] 回應時間
- [ ] 錯誤率（從 Logs）

## 🎉 部署完成

恭喜！你的台灣 ETF 投資分析系統已成功部署到 Render。

記得：
- [ ] 分享你的應用 URL
- [ ] 設定自訂網域（可選）
- [ ] 定期備份重要資料
- [ ] 關注 Render 的免費額度使用情況

---

## 📝 部署資訊記錄

填寫以下資訊以便日後參考：

- **應用名稱**：________________________
- **Render URL**：________________________
- **部署日期**：________________________
- **GitHub Repo**：________________________
- **最後更新**：________________________

---

需要幫助？查看 [README.md](README.md) 或 Render 的[官方文件](https://render.com/docs)。
