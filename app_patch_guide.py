# app.py 整合指南
# 將新版 github_cache.py 套用至 app.py 的修改清單
# ============================================================

# ── 步驟 1：在頂部 import 區（約第 15–18 行後）加入 ─────────
# 原有：
#   from data_fetcher import ETFDataFetcher, get_data_dir, POPULAR_STOCKS, calc_technical_indicators
# 在該行之後加入：

from github_cache import CacheManager, start_scheduler

# ── 步驟 2：在 DATA_DIR 初始化後建立 CacheManager（約第 48 行後）加入 ──
# 原有：
#   DATA_DIR = get_data_dir()
#   print(f"資料目錄: {DATA_DIR}")
#   os.makedirs(DATA_DIR, exist_ok=True)
# 在 os.makedirs 那行之後加入：

cache_mgr = CacheManager(data_dir=DATA_DIR)

# ── 步驟 3：啟動背景排程（在 __main__ 區塊，app.run 之前加入）──
# 找到（約第 2729 行）：
#   if __name__ == '__main__':
#       port = int(os.environ.get('PORT', 5000))
# 在 app.run(...) 之前加入：

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    # ... 既有的 print 不動 ...

    # ▼▼▼ 新增：啟動每日 13:30 自動更新排程 ▼▼▼
    start_scheduler(
        cache=cache_mgr,
        fetcher_factory=lambda: ETFDataFetcher(output_dir=DATA_DIR),
        watchlist_reader=_wl_read,          # 自選股清單
        popular_stocks=list(POPULAR_STOCKS.keys()) if isinstance(POPULAR_STOCKS, dict)
                       else list(POPULAR_STOCKS),
    )
    # ▲▲▲

    host = '0.0.0.0' if os.environ.get('RENDER') else '127.0.0.1'
    app.run(debug=False, host=host, port=port)


# ── 步驟 4：修改 /api/efficient_frontier（約第 1602–1650 行）──
# 找到這段（GitHubCache 三層快取邏輯），整段替換如下：
#
# 原有：
#   try:
#       from github_cache import GitHubCache
#       _ef_gh = GitHubCache()
#   except ImportError:
#       _ef_gh = None
#
#   # ── L1: Render /tmp 快取 ─────────────────────────────
#   prices = {}
#   cached_from = {}
#   for tk in tickers:
#       tmp_path = os.path.join(DATA_DIR, f"{tk}_price.csv")
#       if os.path.exists(tmp_path):
#           ...（本機讀取邏輯）
#
#   # ── L2: GitHub 持久化快取 ────────────────────────────
#   for tk in tickers:
#       if tk in prices: continue
#       if _ef_gh and _ef_gh.enabled and _ef_gh.is_fresh(tk, 'price'):
#           ...（GitHub 讀取邏輯）
#
# 改成（整段替換）：

prices = {}
cached_from = {}
for tk in tickers:
    rows = cache_mgr.get_price(tk, fetcher=None)   # 先不帶 fetcher（不觸發網路）
    if rows and len(rows) >= 20:
        try:
            date_col  = next((c for c in rows[0] if c in ['日期','date','Date']), None)
            close_col = next((c for c in rows[0] if c in ['收盤價','close','Close']), None)
            if date_col and close_col:
                s = pd.Series(
                    [float(r[close_col]) for r in rows],
                    index=pd.to_datetime([r[date_col] for r in rows])
                ).dropna()
                if len(s) >= 20:
                    prices[tk] = s
                    cached_from[tk] = 'local/github'
        except Exception:
            pass

# L3: 網路抓取（只補尚未命中的）
need_fetch = [tk for tk in tickers if tk not in prices]
if cached_from:
    print(f"  [EF] 快取命中: {cached_from}")
if need_fetch:
    print(f"  [EF] 需從 yfinance 下載: {need_fetch}")

# ... （原有的 yfinance 批次下載邏輯不動）...

# 下載完後存檔：把原有的「存快取」區塊改成：
for tk in need_fetch:
    if tk not in prices:
        continue
    price_series = prices[tk]
    price_list = [
        {'date': str(d)[:10], 'close': round(float(v), 2)}
        for d, v in price_series.items() if pd.notna(v)
    ]
    # 同時存本機 + GitHub（cache_mgr 內部處理）
    from github_cache import local_save_price, gh_save_price
    local_save_price(DATA_DIR, tk, price_list)
    gh_save_price(tk, price_list)


# ── 步驟 5（選用）：stock_analysis 也套用三層快取 ─────────────
# 在 /api/stock_analysis/<ticker>（約第 348 行）
# 目前直接呼叫 fetcher.fetch_stock_analysis(ticker)，
# 如果 fetch_stock_analysis 內部可以拆出 price / dividend / fundamental
# 就可以改成：
#
#   price_rows = cache_mgr.get_price(ticker, fetcher)
#   div_rows   = cache_mgr.get_dividend(ticker, fetcher)
#   info       = cache_mgr.get_fundamental(ticker, fetcher)
#
# 但需確認 ETFDataFetcher 有這三個獨立方法。
# 若無，暫時保持原呼叫，只讓 efficient_frontier 套用三層即可。


# ── 步驟 6：安裝相依套件 ────────────────────────────────────
# requirements.txt 加入：
#   apscheduler>=3.10
#   pytz
#
# 安裝指令：
#   pip install apscheduler pytz
#
# 若 Render 上已有 requirements.txt，直接加上這兩行即可。


# ── 步驟 7：環境變數設定 ────────────────────────────────────
# .env（本機）或 Render Dashboard → Environment：
#
#   GH_CACHE_TOKEN=ghp_xxxxxxxx   # 有值時才啟用 GitHub 寫入
#                                 # 沒有 token 時，程式仍可讀 public repo
#                                 # 只是不會自動 push 更新回 GitHub
#
# GH_CACHE_REPO 不再需要設定（已固定為 shui1133/analysis_ETF）


# ── 行為說明 ────────────────────────────────────────────────
# 
#  有人呼叫 API 時的流程：
#    1. 本機有效快取（TTL 內 + 檔案存在）→ 直接回傳（最快）
#    2. GitHub 有效快取 → 讀取並回填本機，再回傳
#    3. 以上都無 → yfinance 抓取，同時存本機 + push GitHub
#
#  每日 13:30（台灣時間）背景排程：
#    - 合併自選股 + POPULAR_STOCKS
#    - 逐一 force_refresh（忽略 TTL，強制重抓）
#    - 存本機 + push GitHub
#
#  本機快取 TTL 判斷邏輯（雙重條件）：
#    - 距上次更新 < 20h（股價）/ 7d（配息）/ 3d（基本面）
#    - 且：若今天已過 13:30 且上次更新在 13:30 之前 → 強制視為過期
#      （確保盤後資料當日能被取到）
