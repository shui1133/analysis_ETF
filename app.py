
# ════════════════════════════════════════════════════════════════
# ★ 將以下程式碼「附加」到 app.py 的最底部（if __name__=='__main__' 之前）
# ════════════════════════════════════════════════════════════════

import time as _time


# ── period_key 輔助函式 ──────────────────────────────────────────
def _get_period_key() -> str:
    """台灣時間 period key，13:30 為當日/昨日分界"""
    from datetime import datetime, timezone, timedelta
    tz_tw = timezone(timedelta(hours=8))
    now = datetime.now(tz_tw)
    if now.hour < 13 or (now.hour == 13 and now.minute < 30):
        now = now - timedelta(days=1)
    return now.strftime('%Y%m%d')


# ─────────────────────────────────────────────────────────────────
# ★ 修復 1：GET /api/stock_cache/<ticker>
#   前端 ghStockCacheLoad() 呼叫此端點，但後端原本根本沒有定義！
# ─────────────────────────────────────────────────────────────────
@app.route('/api/stock_cache/<ticker>', methods=['GET'])
def stock_cache(ticker: str):
    from github_cache import _gh_raw_get
    import json as _json

    ticker = ticker.strip().upper()
    period_key = _get_period_key()
    gh_path = f"ai_reports/{ticker}/{period_key}.json"

    try:
        content = _gh_raw_get(gh_path)
        if content:
            data = _json.loads(content)
            return jsonify({'status': 'cached', 'period_key': period_key, 'data': data})
    except Exception as e:
        print(f"  [stock_cache] 讀取失敗 {ticker}: {e}")

    return jsonify({'status': 'not_found', 'period_key': period_key, 'data': None})


# ─────────────────────────────────────────────────────────────────
# ★ 修復 2：GET + POST /api/ai_report/<ticker>
#   路由片段未貼入主 app.py，導致 404
# ─────────────────────────────────────────────────────────────────
@app.route('/api/ai_report/<ticker>', methods=['GET', 'POST'])
def ai_report(ticker: str):
    from github_cache import _gh_raw_get, _gh_writer
    import requests as _req
    import json as _json

    ticker = ticker.strip().upper()
    period_key = _get_period_key()
    gh_path = f"ai_reports/{ticker}/{period_key}.json"

    # GET：只查快取
    if request.method == 'GET':
        try:
            content = _gh_raw_get(gh_path)
            if content:
                return jsonify({'status': 'cached', 'period_key': period_key,
                                'data': _json.loads(content)})
        except Exception:
            pass
        return jsonify({'status': 'not_found', 'period_key': period_key, 'data': None})

    # POST：先查快取，無快取才呼叫 Claude
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': '伺服器未設定 ANTHROPIC_API_KEY 環境變數'}), 500

    try:
        # ① 查 GitHub 快取，避免重複呼叫 Claude
        try:
            content = _gh_raw_get(gh_path)
            if content:
                return jsonify({'status': 'cached', 'period_key': period_key,
                                'data': _json.loads(content)})
        except Exception:
            pass

        # ② 呼叫 Claude API
        payload = request.get_json(force=True)
        resp = _req.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Content-Type': 'application/json',
                'x-api-key': api_key,
                'anthropic-version': '2023-06-01',
            },
            json=payload,
            timeout=90
        )
        if not resp.ok:
            return jsonify({'error': f'Claude API 錯誤：{resp.status_code}'}), resp.status_code

        # ③ 解析回應
        report_text = ''.join(
            b.get('text', '') for b in resp.json().get('content', [])
            if b.get('type') == 'text'
        )
        if not report_text:
            return jsonify({'error': '取得的 AI 分析內容為空'}), 500

        # ④ 存入 GitHub
        from datetime import datetime, timezone, timedelta
        tz_tw = timezone(timedelta(hours=8))
        report_data = {
            'ticker': ticker,
            'period_key': period_key,
            'report_text': report_text,
            'generated_at': datetime.now(tz_tw).isoformat(),
        }
        try:
            _gh_writer.put(gh_path, _json.dumps(report_data, ensure_ascii=False, indent=2),
                           f'ai_report: {ticker} {period_key}')
        except Exception as e:
            print(f'  [ai_report] GitHub 存檔失敗（非致命）: {e}')

        return jsonify({'status': 'generated', 'period_key': period_key, 'data': report_data})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# GET /api/cache_version
# ─────────────────────────────────────────────────────────────────
@app.route('/api/cache_version', methods=['GET'])
def cache_version():
    v = getattr(app, '_cache_version', '')
    if not v:
        v = str(int(_time.time()))
        app._cache_version = v
    return jsonify({'version': v})
