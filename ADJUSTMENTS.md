# 🔧 重要調整說明

## 📝 已調整的內容

### 1. **資料目錄邏輯** ✅
**檔案**：`data_fetcher.py`, `backtest.py`

**原本問題**：
- 在 Linux 環境使用 `/data` 目錄
- Render 上沒有寫入 `/data` 的權限

**已調整**：
```python
def get_data_dir():
    if os.environ.get('RENDER'):
        data_dir = "/tmp/data"  # Render 暫存目錄
    elif platform.system() == 'Windows':
        data_dir = r"C:\Python\退休理財規劃分析\data"
    else:
        data_dir = os.path.join(os.path.dirname(__file__), "data")
```

**影響**：
- ✅ 支援 Render 部署
- ✅ 保持本地開發環境相容
- ⚠️ Render 上的資料在重啟後會消失（需要重新爬取）

---

### 2. **Flask 應用啟動邏輯** ✅
**檔案**：`app.py`

**已調整**：
```python
# 從環境變數取得 PORT
port = int(os.environ.get('PORT', 5000))

# Render 環境使用 0.0.0.0，本地使用 127.0.0.1
host = '0.0.0.0' if os.environ.get('RENDER') else '127.0.0.1'

# 關閉 debug 模式（生產環境）
app.run(debug=False, host=host, port=port)
```

**為什麼重要**：
- Render 會自動設定 `PORT` 環境變數
- 必須綁定 `0.0.0.0` 才能被外部訪問
- 生產環境不應該開啟 debug 模式

---

### 3. **新增部署相關檔案** ✅

#### `requirements.txt`
列出所有 Python 依賴：
```
Flask==3.0.0
pandas==2.1.4
numpy==1.26.2
yfinance==0.2.35
requests==2.31.0
beautifulsoup4==4.12.2
lxml==5.1.0
gunicorn==21.2.0  # WSGI server
```

#### `Procfile`
告訴 Render 如何啟動應用：
```
web: gunicorn app:app
```

#### `runtime.txt`
指定 Python 版本：
```
python-3.11.0
```

#### `render.yaml`
自動部署設定：
```yaml
services:
  - type: web
    name: taiwan-etf-analyzer
    env: python
    region: singapore
    plan: free
```

---

## ⚠️ 需要注意的限制

### 1. **資料持久化問題**（重要！）

**現況**：
- Render 免費方案使用 `/tmp` 暫存目錄
- 服務重啟後資料會消失
- 每次需要重新爬取 ETF 資料

**解決方案選項**：

**選項 A：接受現況**（最簡單）
- 每次使用前先爬取資料
- 適合低頻率使用

**選項 B：升級到付費方案**（$7/月起）
- 獲得持久化磁碟空間
- 資料不會消失

**選項 C：整合資料庫**（推薦長期方案）
- 使用 Render 的 PostgreSQL（免費方案可用）
- 需要修改程式碼儲存資料到資料庫
- 參考實作範例：

```python
# 在 data_fetcher.py 加入資料庫儲存
import psycopg2

class ETFDataFetcher:
    def save_to_db(self, ticker, data):
        conn = psycopg2.connect(os.environ['DATABASE_URL'])
        cur = conn.cursor()
        # 儲存邏輯...
```

---

### 2. **休眠機制**

**現象**：
- 15 分鐘無活動會休眠
- 首次訪問需要 30-60 秒喚醒

**解決方案**：

**選項 A：接受休眠**（免費）
- 首次訪問耐心等待

**選項 B：定時 Ping**（免費）
- 使用 UptimeRobot 等服務定時訪問
- 保持服務常駐
- 注意：可能違反 Render 使用條款

**選項 C：升級方案**（付費）
- 付費方案不會休眠

---

### 3. **爬蟲穩定性**

**潛在問題**：
- yfinance 可能被限速
- MoneyDJ、Goodinfo 有反爬蟲
- 資料來源可能暫時無法訪問

**已實作的保護**：
- ✅ 多資料源備援
- ✅ 失敗時使用模擬資料
- ✅ 適當的延遲避免被封鎖

**建議**：
- 在非尖峰時段爬取
- 不要頻繁爬取同一 ETF
- 考慮加入 Redis 快取

---

## 🚀 進階優化建議

### 1. **加入 Redis 快取**

```yaml
# render.yaml 加入
services:
  - type: redis
    name: etf-cache
    plan: free
```

```python
# 在 app.py 使用
import redis
cache = redis.from_url(os.environ.get('REDIS_URL'))
```

### 2. **整合 PostgreSQL**

```yaml
# render.yaml 加入
databases:
  - name: etf-database
    plan: free
```

### 3. **設定 Cron Job 定期爬取**

```yaml
# render.yaml 加入
services:
  - type: cron
    name: data-fetcher
    schedule: "0 2 * * *"  # 每天凌晨 2 點
    buildCommand: pip install -r requirements.txt
    startCommand: python scheduled_fetch.py
```

---

## 📋 部署前最後檢查

- [ ] 所有檔案都已準備好
- [ ] `templates/` 資料夾包含 `index.html`
- [ ] Git 已初始化並推送到 GitHub
- [ ] 已讀過 [DEPLOYMENT_CHECKLIST.md](DEPLOYMENT_CHECKLIST.md)
- [ ] 了解免費方案的限制
- [ ] 準備好接受首次訪問的等待時間

---

## 🆘 遇到問題？

### 部署失敗

1. 檢查 Render 的 Build Logs
2. 確認 `requirements.txt` 格式正確
3. 確認 Python 版本相容

### 執行錯誤

1. 檢查 Deploy Logs 和 Service Logs
2. 確認環境變數設定正確
3. 測試本地環境是否正常

### 功能異常

1. 檢查資料是否成功爬取
2. 查看瀏覽器 Console 的錯誤訊息
3. 確認 API 回應正常

---

## 📞 需要協助

如果遇到任何問題：
1. 查看 [README.md](README.md)
2. 參考 [Render 官方文件](https://render.com/docs)
3. 檢查 GitHub Issues
4. 搜尋相關錯誤訊息

---

祝部署順利！🎉
