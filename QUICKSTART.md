# ⚡ 快速開始：5 分鐘部署到 Render

## 步驟 1：準備 GitHub Repository (1 分鐘)

```bash
# 初始化 Git
git init
git add .
git commit -m "Initial commit"

# 在 GitHub 建立新 repository，然後：
git remote add origin https://github.com/你的用戶名/你的repo名稱.git
git branch -M main
git push -u origin main
```

## 步驟 2：連接 Render (1 分鐘)

1. 前往 https://dashboard.render.com/
2. 點擊 "New +" → "Blueprint"
3. 選擇你剛建立的 GitHub repository
4. 點擊 "Apply"

## 步驟 3：等待部署 (3 分鐘)

Render 會自動：
- ✅ 讀取 `render.yaml` 設定
- ✅ 安裝 Python 依賴
- ✅ 啟動你的應用

## 步驟 4：測試應用

1. 點擊 Render 提供的 URL
2. 等待 30 秒（首次喚醒）
3. 開始使用！

---

## 🎯 就這麼簡單！

你的台灣 ETF 投資分析系統已經上線了！

**下一步：**
- 📖 詳細文件：查看 [README.md](README.md)
- ✅ 完整檢查清單：查看 [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)
- 🐛 遇到問題？查看文件中的「常見問題」章節

---

## ⚠️ 重要提醒

1. **免費方案限制**：15 分鐘無活動會休眠
2. **資料儲存**：重啟後需要重新爬取資料（暫存檔案系統）
3. **首次訪問**：需要 30-60 秒喚醒時間

---

需要更多幫助？
- Render 官方文件：https://render.com/docs
- 專案 README：[README.md](README.md)
