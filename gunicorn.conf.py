# gunicorn.conf.py
# 放在專案根目錄，Render 啟動時自動讀取
# 用途：解決 force_refresh_price 呼叫 yfinance 時 worker timeout 問題

import multiprocessing

# Worker 設定
workers     = 2                          # Render 免費方案 512MB RAM，2 workers 足夠
worker_class = "sync"                    # sync worker（不需要 gevent）
threads     = 2                          # 每個 worker 2 threads

# Timeout 設定（關鍵修正）
timeout     = 120                        # worker timeout 從預設 30s 延長到 120s
graceful_timeout = 30                    # 優雅關閉等待時間
keepalive   = 5

# 綁定
bind        = "0.0.0.0:10000"           # Render 預設 port

# 日誌
loglevel    = "info"
accesslog   = "-"                        # stdout
errorlog    = "-"                        # stderr
access_log_format = '%(h)s [%(t)s] "%(r)s" %(s)s %(b)s'

# 預載入 app（加快 worker fork 速度）
preload_app = True
