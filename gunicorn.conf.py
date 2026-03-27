# gunicorn.conf.py ── 最終版（含排程 hook）
# 放在專案根目錄，Render 啟動時自動讀取
#
# 修正紀錄：
#   v1 - 初版：timeout 90s、2 workers、2 threads
#   v2 - 修正：GitHub push 同步阻塞造成 worker timeout
#   v3 - 新增：on_starting hook，在 Render 環境正確啟動每日 13:30 排程
#        （gunicorn 不走 __main__，需用此 hook）

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


# ══════════════════════════════════════════════════════════════
# on_starting hook：gunicorn master 啟動時執行一次（僅 master 執行）
# 解決 Render 環境下排程不觸發的問題（preload_app=True 不走 __main__）
# ══════════════════════════════════════════════════════════════

def on_starting(server):
    """
    gunicorn master process 啟動時自動執行。
    在此啟動 APScheduler 背景排程，確保每日 13:30（台灣時間）
    自動更新熱門股票及自選股快取。

    注意：preload_app=True 情況下，此 hook 在 app 模組已載入後執行，
    可安全 import app.py 中的物件。
    """
    import json
    import logging

    logger = logging.getLogger("gunicorn.error")
    logger.info("[gunicorn on_starting] 初始化快取排程...")

    try:
        from data_fetcher import ETFDataFetcher, get_data_dir, POPULAR_STOCKS
        from github_cache import CacheManager, start_scheduler

        DATA_DIR  = get_data_dir()
        cache_mgr = CacheManager(data_dir=DATA_DIR)

        def _wl_read():
            """
            讀取自選股清單。
            預期格式：watchlist.json = ["2330", "2317", ...]
            若不存在則回傳空清單（排程仍會處理 POPULAR_STOCKS）
            """
            try:
                wl_path = os.path.join(DATA_DIR, 'watchlist.json')
                if os.path.exists(wl_path):
                    with open(wl_path, encoding='utf-8') as f:
                        wl = json.load(f)
                    # 支援 [{"code": "2330", ...}] 或 ["2330", ...] 兩種格式
                    if wl and isinstance(wl[0], dict):
                        return [item.get('code', '') for item in wl if item.get('code')]
                    return [str(x) for x in wl if x]
            except Exception as e:
                logger.warning("[排程] 讀取自選股失敗（非致命）: %s", e)
            return []

        # 將 POPULAR_STOCKS 統一轉為代碼清單
        # 支援 [{"code": "2330", ...}] 或 ["2330", ...] 兩種格式
        if POPULAR_STOCKS and isinstance(POPULAR_STOCKS[0], dict):
            popular_list = [s['code'] for s in POPULAR_STOCKS if s.get('code')]
        else:
            popular_list = list(POPULAR_STOCKS)

        scheduler = start_scheduler(
            cache=cache_mgr,
            fetcher_factory=lambda: ETFDataFetcher(output_dir=DATA_DIR),
            watchlist_reader=_wl_read,
            popular_stocks=popular_list,
        )

        if scheduler:
            logger.info(
                "[gunicorn on_starting] ✅ 排程已啟動，"
                "每日 13:30 TW 自動更新 %d 支熱門股票", len(popular_list)
            )
        else:
            logger.warning(
                "[gunicorn on_starting] ⚠️  排程啟動失敗（可能缺少 apscheduler）"
            )

    except Exception as e:
        # 排程失敗不應阻斷 server 啟動，只記錄 warning
        logger.warning("[gunicorn on_starting] 排程初始化失敗（非致命）: %s", e)
