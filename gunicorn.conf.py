# gunicorn.conf.py
# 放在專案根目錄，Render 啟動時自動讀取
# 修正：解決 GitHub push 同步阻塞造成 worker timeout 問題

import os

# ── Worker 設定 ────────────────────────────────────────────────
workers      = 2          # Render 免費方案 512MB RAM，2 workers 足夠
worker_class = "sync"     # sync worker（不需要 gevent）
threads      = 2          # 每個 worker 2 threads，提升並行能力

# ── Timeout 設定（關鍵）───────────────────────────────────────
# yfinance 抓取最多約 25s，本機存檔約 1s，設 90s 有充足緩衝
# GitHub push 已改為背景執行緒，不計入此 timeout
timeout          = 90     # worker 無回應超過 90s 才 kill（原本預設 30s）
graceful_timeout = 30     # 優雅關閉等待時間
keepalive        = 5

# ── 綁定（讀取 Render 注入的 PORT 環境變數）──────────────────
_port = os.environ.get('PORT', '10000')
bind  = f"0.0.0.0:{_port}"

# ── 日誌 ──────────────────────────────────────────────────────
loglevel          = "info"
accesslog         = "-"    # stdout
errorlog          = "-"    # stderr
access_log_format = '%(h)s [%(t)s] "%(r)s" %(s)s %(b)s %(L)ss'

# ── 預載入（加快 worker fork 速度，共享記憶體初始化）─────────
# 注意：preload_app=True 時，__name__ == 'app' 而非 '__main__'
# app.py 已用 os.environ.get('RENDER') 判斷環境，不影響
preload_app = True
