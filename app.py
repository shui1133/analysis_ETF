"""
════════════════════════════════════════════════════════════════════
修復說明：將以下兩個路由貼入 app.py

問題 1：/api/stock_cache/<ticker> → 404（路由根本不存在）
問題 2：/api/ai_report/<ticker>  → 404（片段未貼入 app.py）
問題 3：_updateQuotaBadge is not defined（前端缺少函式定義）

【貼入位置】緊接在 /api/claude_proxy 路由結尾的 } 之後
════════════════════════════════════════════════════════════════════
"""

# ─────────────────────────────────────────────────────────────────
# ★ 修復 1：GET /api/stock_cache/<ticker>
#   前端 ghStockCacheLoad() 呼叫此端點，後端原先根本沒有定義！
# ─────────────────────────────────────────────────────────────────
@app.route('/api/stock_cache/<ticker>', methods=['GET'])
def stock_cache(ticker: str):
    """
    讀取 GitHub 快取的股票分析資料。
    GET /api/stock_cache/<ticker>
      回傳 { status: 'cached'|'not_found', data, period_key }
    """
    from github_cache import _gh_raw_get
    import json as _json

    ticker = ticker.strip().upper()

    # ── period_key 與 ai_report 共用同一邏輯 ──
    from datetime import datetime, timezone, timedelta
    tz_tw = timezone(timedelta(hours=8))
    now = datetime.now(tz_tw)
    if now.hour < 13 or (now.hour == 13 and now.minute < 30):
        now = now - timedelta(days=1)
    period_key = now.strftime('%Y%m%d')

    # 嘗試讀取今日 AI 報告快取
    gh_path = f"ai_reports/{ticker}/{period_key}.json"
    try:
        content = _gh_raw_get(gh_path)
        if content:
            data = _json.loads(content)
            return jsonify({
                'status': 'cached',
                'period_key': period_key,
                'data': data
            })
    except Exception as e:
        print(f"  [stock_cache] 讀取失敗 {ticker}: {e}")

    return jsonify({
        'status': 'not_found',
        'period_key': period_key,
        'data': None
    })


# ─────────────────────────────────────────────────────────────────
# ★ 修復 2：GET + POST /api/ai_report/<ticker>
#   此路由已在 app.py 片段中定義，但尚未貼入主 app.py，
#   直接複製自 app.py 上傳的檔案（保持原樣）
# ─────────────────────────────────────────────────────────────────
import time as _time

def _ai_report_gh_path(ticker: str, period_key: str) -> str:
    return f"ai_reports/{ticker}/{period_key}.json"


def _get_period_key() -> str:
    """回傳台灣時間的 period key（13:30 為當日/昨日分界）"""
    from datetime import datetime, timezone, timedelta
    tz_tw = timezone(timedelta(hours=8))
    now = datetime.now(tz_tw)
    if now.hour < 13 or (now.hour == 13 and now.minute < 30):
        now = now - timedelta(days=1)
    return now.strftime('%Y%m%d')


@app.route('/api/ai_report/<ticker>', methods=['GET', 'POST'])
def ai_report(ticker: str):
    """
    AI 財務健診報告端點。
    GET  → 回傳 { status: 'cached'|'not_found', data, period_key }
    POST → 呼叫 Claude API，存 GitHub，回傳 { status: 'generated', data }
    """
    import requests as _req
    from github_cache import _gh_raw_get, _gh_writer

    ticker = ticker.strip().upper()
    period_key = _get_period_key()
    gh_path = _ai_report_gh_path(ticker, period_key)

    # ── GET：只查快取 ────────────────────────────────────────────
    if request.method == 'GET':
        try:
            content = _gh_raw_get(gh_path)
            if content:
                import json as _json
                data = _json.loads(content)
                return jsonify({
                    'status': 'cached',
                    'period_key': period_key,
                    'data': data
                })
        except Exception:
            pass
        return jsonify({
            'status': 'not_found',
            'period_key': period_key,
            'data': None
        })

    # ── POST：產生報告 ────────────────────────────────────────────
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': '伺服器未設定 ANTHROPIC_API_KEY 環境變數'}), 500

    try:
        # ① 先查 GitHub 快取，避免重複呼叫 Claude
        try:
            content = _gh_raw_get(gh_path)
            if content:
                import json as _json
                data = _json.loads(content)
                return jsonify({'status': 'cached', 'period_key': period_key, 'data': data})
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

        claude_resp = resp.json()

        # ③ 解析 Claude 回應文字
        report_text = ''
        for block in claude_resp.get('content', []):
            if block.get('type') == 'text':
                report_text += block.get('text', '')

        if not report_text:
            return jsonify({'error': '取得的 AI 分析內容為空'}), 500

        # ④ 存入 GitHub（失敗不影響回傳）
        from datetime import datetime, timezone, timedelta
        import json as _json
        tz_tw = timezone(timedelta(hours=8))
        generated_at = datetime.now(tz_tw).isoformat()

        report_data = {
            'ticker': ticker,
            'period_key': period_key,
            'report_text': report_text,
            'generated_at': generated_at,
        }

        try:
            _gh_writer.put(
                gh_path,
                _json.dumps(report_data, ensure_ascii=False, indent=2),
                f'ai_report: {ticker} {period_key}'
            )
        except Exception as e:
            print(f'  [ai_report] GitHub 存檔失敗（非致命）: {e}')

        return jsonify({
            'status': 'generated',
            'period_key': period_key,
            'data': report_data
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# GET /api/cache_version（已在原始 app.py 片段中，確保也貼入）
# ─────────────────────────────────────────────────────────────────
@app.route('/api/cache_version', methods=['GET'])
def cache_version():
    v = getattr(app, '_cache_version', '')
    if not v:
        v = str(int(_time.time()))
        app._cache_version = v
    return jsonify({'version': v})
