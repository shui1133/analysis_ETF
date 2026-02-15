# 📦 專案檔案清單

## 🎯 部署到 Render 所需的核心檔案

### 必要檔案（缺一不可）

| 檔案 | 說明 | 用途 |
|------|------|------|
| **app.py** | Flask 主程式 | 應用程式進入點 |
| **data_fetcher.py** | 資料爬取模組 | ETF 資料抓取功能 |
| **backtest.py** | 回測分析模組 | 投資組合回測計算 |
| **requirements.txt** | Python 依賴清單 | 告訴 Render 需要安裝哪些套件 |
| **Procfile** | 啟動指令 | 告訴 Render 如何啟動應用 |
| **render.yaml** | Render 設定 | 自動部署配置（推薦） |
| **runtime.txt** | Python 版本 | 指定使用 Python 3.11.0 |
| **templates/index.html** | 前端介面 | 使用者介面 HTML |

### 文件檔案（建議包含）

| 檔案 | 說明 | 重要性 |
|------|------|--------|
| **README.md** | 專案說明文件 | ⭐⭐⭐ 必讀 |
| **QUICKSTART.md** | 5 分鐘快速部署指南 | ⭐⭐⭐ 新手必讀 |
| **DEPLOYMENT_CHECKLIST.md** | 部署檢查清單 | ⭐⭐ 建議使用 |
| **ADJUSTMENTS.md** | 重要調整說明 | ⭐⭐ 了解系統限制 |
| **FILES_MANIFEST.md** | 本檔案清單 | ⭐ 參考用 |

### 測試檔案（開發用，可選）

| 檔案 | 說明 | 用途 |
|------|------|------|
| **test_crawlers.py** | 爬蟲測試腳本 | 本地測試資料來源 |
| **test_system.py** | 系統測試腳本 | 本地測試完整流程 |

### Git 相關（建議包含）

| 檔案 | 說明 | 用途 |
|------|------|------|
| **.gitignore** | Git 忽略規則 | 避免上傳不必要的檔案 |

---

## 📁 完整目錄結構

```
taiwan-etf-analyzer/
├── app.py                          # Flask 主程式 [必要]
├── data_fetcher.py                 # 資料爬取模組 [必要]
├── backtest.py                     # 回測分析模組 [必要]
├── requirements.txt                # Python 依賴 [必要]
├── Procfile                        # Render 啟動設定 [必要]
├── render.yaml                     # Render 部署設定 [必要]
├── runtime.txt                     # Python 版本 [必要]
├── .gitignore                      # Git 忽略清單 [建議]
│
├── templates/                      # Flask 模板目錄 [必要]
│   └── index.html                  # 前端介面 [必要]
│
├── README.md                       # 專案說明 [建議]
├── QUICKSTART.md                   # 快速開始 [建議]
├── DEPLOYMENT_CHECKLIST.md         # 部署清單 [建議]
├── ADJUSTMENTS.md                  # 調整說明 [建議]
├── FILES_MANIFEST.md               # 本檔案 [參考]
│
├── test_crawlers.py                # 測試腳本 [開發用]
└── test_system.py                  # 測試腳本 [開發用]
```

---

## 🚀 最小部署需求

如果你想要最精簡的部署，至少需要這些檔案：

```
必要檔案（8 個）：
✅ app.py
✅ data_fetcher.py
✅ backtest.py
✅ requirements.txt
✅ Procfile
✅ render.yaml
✅ runtime.txt
✅ templates/index.html
```

---

## 📖 閱讀順序建議

第一次部署請按此順序閱讀：

1. **QUICKSTART.md** - 5 分鐘快速了解部署流程
2. **ADJUSTMENTS.md** - 了解已調整的內容和限制
3. **DEPLOYMENT_CHECKLIST.md** - 跟著清單一步步操作
4. **README.md** - 詳細的專案文件

---

## ⚙️ 檔案用途詳解

### requirements.txt
```
Flask==3.0.0           # Web 框架
pandas==2.1.4          # 資料處理
numpy==1.26.2          # 數值計算
yfinance==0.2.35       # Yahoo Finance API
requests==2.31.0       # HTTP 請求
beautifulsoup4==4.12.2 # HTML 解析
lxml==5.1.0            # XML/HTML 解析器
gunicorn==21.2.0       # WSGI Server（生產環境）
```

### Procfile
```
web: gunicorn app:app
```
- `web`：Render 的服務類型
- `gunicorn`：生產級 Python WSGI HTTP Server
- `app:app`：指向 app.py 中的 Flask app 物件

### render.yaml
```yaml
services:
  - type: web              # Web 服務
    name: taiwan-etf-analyzer
    env: python            # Python 環境
    region: singapore      # 新加坡節點（離台灣近）
    plan: free            # 免費方案
    buildCommand: pip install -r requirements.txt
    startCommand: gunicorn app:app
```

### runtime.txt
```
python-3.11.0
```
指定使用 Python 3.11.0 版本

---

## 🔍 檔案檢查清單

部署前確認：

**Python 程式檔案**
- [ ] app.py 存在且可執行
- [ ] data_fetcher.py 存在
- [ ] backtest.py 存在

**設定檔案**
- [ ] requirements.txt 格式正確（每行一個套件）
- [ ] Procfile 內容正確
- [ ] render.yaml 設定完整
- [ ] runtime.txt 版本正確

**HTML 模板**
- [ ] templates/ 目錄存在
- [ ] templates/index.html 存在

**文件檔案**（建議）
- [ ] README.md 已閱讀
- [ ] QUICKSTART.md 已閱讀
- [ ] .gitignore 已包含

---

## 📊 檔案大小參考

```
app.py              ~10 KB
data_fetcher.py     ~23 KB
backtest.py         ~20 KB
index.html          ~30 KB
requirements.txt    < 1 KB
Procfile            < 1 KB
render.yaml         < 1 KB
runtime.txt         < 1 KB

總計：約 85 KB
```

---

## ✅ 最終檢查

部署前最後確認：

- [ ] 所有必要檔案都已準備
- [ ] templates 目錄結構正確
- [ ] Python 檔案語法正確（可用 `python -m py_compile` 檢查）
- [ ] requirements.txt 沒有拼寫錯誤
- [ ] 已閱讀 QUICKSTART.md
- [ ] 已準備 GitHub repository
- [ ] 已註冊 Render 帳號

---

準備好了嗎？開始部署吧！🚀

參考 [QUICKSTART.md](QUICKSTART.md) 開始 5 分鐘快速部署！
