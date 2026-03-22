"""
ma_analysis_enhanced.py  ── V3 強化版移動平均線分析模組
════════════════════════════════════════════════════════════
新增功能：
  1. 葛蘭碧八大法則完整判斷（標記觸發的法則編號）
  2. 死亡交叉 / 黃金交叉距離預測（估算 N 天後出現）
  3. 乖離率（BIAS）計算與警戒判斷
  4. 均線多空排列判斷（5/10/20/60 完整排列）
  5. 均線支撐壓力強度評分
  6. 強化版 _calc_trend()  ── 整合上述所有分析
  7. 強化版 _generate_recommendation()  ── 納入葛蘭碧分析
  8. 評級門檻重新校準  ── 技術面上限約 ±16（不截斷，直接累加）
     門檻：強力買進≥12／買進≥6／持有≥0／減碼≥-6／賣出<-6

使用方式：
  from ma_analysis_enhanced import (
      analyze_ma,
      calc_granville_signals,
      estimate_cross_days,
      calc_bias,
      enhanced_calc_trend,
      enhanced_generate_recommendation,
  )

  # 在 get_stock_analysis() 中替換原有函數：
  trend          = enhanced_calc_trend(last['close'], latest_ind, ohlcv)
  recommendation = enhanced_generate_recommendation(
                      ticker, last['close'], latest_ind, trend,
                      chip, info, div_yield, support, resist)
════════════════════════════════════════════════════════════
"""

from __future__ import annotations
import numpy as np
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# 1. 乖離率（BIAS）
# ══════════════════════════════════════════════════════════════════════════════

def calc_bias(close: float, ma_value: float | None) -> float | None:
    """
    乖離率 = (現價 - MAn) / MAn × 100  (%)
    正值 → 股價高於均線（正乖離）；負值 → 低於均線（負乖離）
    """
    if not ma_value or ma_value == 0:
        return None
    return round((close - ma_value) / ma_value * 100, 2)


def bias_warning(bias: float | None, ma_period: int = 20) -> dict:
    """
    依乖離率給出警示等級。
    警戒值參考（以 MA20 為例）：
      正乖離 > +5%   → 超買警示
      負乖離 < -5%   → 超賣機會
    對不同週期均線乘以比例係數。
    """
    if bias is None:
        return {'level': 'neutral', 'text': '無資料', 'color': '#94a3b8'}

    # 週期越長，合理乖離越大，係數調整
    scale = {5: 0.5, 10: 0.7, 20: 1.0, 60: 2.0, 120: 3.0, 200: 4.0}.get(ma_period, 1.0)
    overbought = 5.0 * scale
    oversold   = -5.0 * scale

    if bias > overbought * 1.5:
        return {'level': 'extreme_overbought', 'text': f'極度超買（{bias:+.1f}%）', 'color': '#b91c1c'}
    elif bias > overbought:
        return {'level': 'overbought',         'text': f'超買警示（{bias:+.1f}%）',  'color': '#dc2626'}
    elif bias < oversold * 1.5:
        return {'level': 'extreme_oversold',   'text': f'極度超賣（{bias:+.1f}%）', 'color': '#15803d'}
    elif bias < oversold:
        return {'level': 'oversold',           'text': f'超賣機會（{bias:+.1f}%）', 'color': '#16a34a'}
    else:
        return {'level': 'normal',             'text': f'正常範圍（{bias:+.1f}%）', 'color': '#0369a1'}


# ══════════════════════════════════════════════════════════════════════════════
# 2. 葛蘭碧八大法則 (Granville's 8 Rules)
# ══════════════════════════════════════════════════════════════════════════════

def calc_granville_signals(
    close_series: list[float],
    ma_series: list[float],
    lookback: int = 5,
) -> list[dict]:
    """
    對最近 lookback 筆資料判斷葛蘭碧法則觸發。
    返回觸發的法則清單，每筆包含：
      rule       : 法則編號 1~8
      signal     : 'buy' | 'sell'
      name       : 中文名稱
      description: 說明
      strength   : 'strong' | 'moderate' | 'weak'
      index      : 觸發位置（-1 = 最新）
    """
    n = min(lookback, len(close_series), len(ma_series))
    if n < 3:
        return []

    prices = close_series[-n:]
    mas    = ma_series[-n:]
    results = []

    # 均線斜率（最近3日）
    def ma_slope(i: int) -> float:
        if i < 2:
            return 0.0
        return mas[i] - mas[i - 2]

    def prev_cross(i: int) -> str:
        """判斷最近一次穿越方向（'up','down','none'）"""
        if i < 1:
            return 'none'
        above_now  = prices[i]  > mas[i]
        above_prev = prices[i-1] > mas[i-1]
        if not above_prev and above_now:
            return 'up'
        if above_prev and not above_now:
            return 'down'
        return 'none'

    for i in range(1, n):
        slope = ma_slope(i)
        cross = prev_cross(i)
        p, m  = prices[i], mas[i]
        bias  = (p - m) / m * 100 if m else 0

        # ── 買進法則 ────────────────────────────────────────────
        # 法則 1：起漲買進——均線由降轉平向上，股價由下往上突破
        if cross == 'up' and slope >= 0:
            results.append({
                'rule': 1, 'signal': 'buy',
                'name': '起漲買進',
                'description': '均線由下降轉平且向上，股價突破均線',
                'strength': 'strong', 'index': i - n,
            })

        # 法則 2：續漲加碼——股價在均線上方，拉回至均線後反彈（未跌破）
        if prices[i-1] > mas[i-1] and cross == 'none' and p > m and bias < 3:
            if i >= 2 and prices[i-1] < prices[i-2]:  # 前日有回調
                results.append({
                    'rule': 2, 'signal': 'buy',
                    'name': '續漲加碼',
                    'description': '股價在均線上方回測均線後反彈',
                    'strength': 'moderate', 'index': i - n,
                })

        # 法則 3：超賣反彈——股價在均線下方，且負乖離過大（超賣）
        if p < m and bias < -6:
            results.append({
                'rule': 3, 'signal': 'buy',
                'name': '超賣反彈',
                'description': f'股價大幅低於均線（乖離 {bias:.1f}%），技術性反彈機會',
                'strength': 'moderate', 'index': i - n,
            })

        # 法則 4：末跌買進——股價從均線上方跌至下方，但均線仍上升
        if cross == 'down' and slope > 0:
            results.append({
                'rule': 4, 'signal': 'buy',
                'name': '末跌買進',
                'description': '股價跌破均線，但均線仍上升，可能是最後一跌',
                'strength': 'weak', 'index': i - n,
            })

        # ── 賣出法則 ────────────────────────────────────────────
        # 法則 5：趨勢轉空賣出——均線由升轉平向下，股價跌破均線
        if cross == 'down' and slope <= 0:
            results.append({
                'rule': 5, 'signal': 'sell',
                'name': '趨勢轉空賣出',
                'description': '均線向下轉折，股價跌破均線，趨勢確認轉空',
                'strength': 'strong', 'index': i - n,
            })

        # 法則 6：超買回吐——股價在均線上方，正乖離過大
        if p > m and bias > 6:
            results.append({
                'rule': 6, 'signal': 'sell',
                'name': '超買回吐',
                'description': f'股價大幅高於均線（乖離 {bias:.1f}%），漲幅過大逢高賣出',
                'strength': 'moderate', 'index': i - n,
            })

        # 法則 7：反彈賣出——股價在均線下方，反彈至均線後再拉回
        if prices[i-1] < mas[i-1] and cross == 'none' and p < m and bias > -3:
            if i >= 2 and prices[i-1] > prices[i-2]:  # 前日有反彈
                results.append({
                    'rule': 7, 'signal': 'sell',
                    'name': '反彈賣出',
                    'description': '股價在均線下方反彈至均線附近，但無法突破，再度下跌',
                    'strength': 'moderate', 'index': i - n,
                })

        # 法則 8：空頭賣出——股價在均線上方，但均線已轉空（空頭行情中的短暫超漲）
        if p > m and slope < 0 and bias > 3:
            results.append({
                'rule': 8, 'signal': 'sell',
                'name': '空頭賣出',
                'description': '均線已向下，股價短暫反彈至均線上方，應逢高減碼',
                'strength': 'weak', 'index': i - n,
            })

    # 只保留最新觸發的（避免重複）
    seen = set()
    unique = []
    for r in reversed(results):
        if r['rule'] not in seen:
            seen.add(r['rule'])
            unique.append(r)
    return list(reversed(unique))


# ══════════════════════════════════════════════════════════════════════════════
# 3. 死亡交叉 / 黃金交叉距離預測
# ══════════════════════════════════════════════════════════════════════════════

def estimate_cross_days(
    close_series: list[float],
    ma_short_period: int = 5,
    ma_long_period: int  = 20,
    forecast_days: int   = 30,
) -> dict:
    """
    預測短期均線與長期均線交叉的估算天數。

    方法：
      1. 計算現有歷史均線陣列
      2. 用最近 N 天的收盤價線性趨勢延伸（簡單線性回歸）預測未來收盤價
      3. 在預測序列上計算滾動均線，找出交叉點

    返回 dict：
      cross_type    : 'golden'（黃金交叉）| 'death'（死亡交叉）| 'none'
      est_days      : 預估天數（None 表示預測期內無交叉）
      est_date_hint : 描述文字
      current_gap   : 當前短均線 - 長均線（正=短>長=多方；負=空方）
      gap_trend     : 'widening'（差距擴大）| 'narrowing'（差距縮小）| 'stable'
      confidence    : 'high' | 'medium' | 'low'
      short_ma_now  : 當前短期均線
      long_ma_now   : 當前長期均線
      details       : 詳細說明
    """
    n = len(close_series)
    if n < max(ma_long_period * 2, 40):
        return {
            'cross_type': 'none', 'est_days': None,
            'est_date_hint': '歷史資料不足，無法預測',
            'current_gap': None, 'gap_trend': 'unknown',
            'confidence': 'low', 'short_ma_now': None, 'long_ma_now': None,
            'details': '需要至少 {} 筆資料'.format(max(ma_long_period * 2, 40)),
        }

    closes = np.array(close_series, dtype=float)

    def rolling_ma(arr: np.ndarray, p: int) -> np.ndarray:
        result = np.full(len(arr), np.nan)
        for i in range(p - 1, len(arr)):
            result[i] = arr[i - p + 1: i + 1].mean()
        return result

    short_ma = rolling_ma(closes, ma_short_period)
    long_ma  = rolling_ma(closes, ma_long_period)

    # 當前均線值
    short_now = float(short_ma[-1])
    long_now  = float(long_ma[-1])
    gap_now   = short_now - long_now

    # 差距趨勢（最近 5 天）
    recent_gaps = short_ma[-6:] - long_ma[-6:]
    valid_gaps  = recent_gaps[~np.isnan(recent_gaps)]
    if len(valid_gaps) >= 3:
        gap_change = float(valid_gaps[-1] - valid_gaps[0])
        if abs(gap_change) < abs(gap_now) * 0.02:
            gap_trend = 'stable'
        elif gap_change * gap_now > 0:   # 同號 → 擴大
            gap_trend = 'widening'
        else:
            gap_trend = 'narrowing'
    else:
        gap_trend = 'unknown'

    # 如果差距擴大或穩定，交叉可能性低
    if gap_trend in ('widening', 'stable') and abs(gap_now) > short_now * 0.03:
        return {
            'cross_type': 'none', 'est_days': None,
            'est_date_hint': f'均線差距{"擴大中" if gap_trend=="widening" else "穩定"}，近期不會出現交叉',
            'current_gap': round(gap_now, 2), 'gap_trend': gap_trend,
            'confidence': 'medium',
            'short_ma_now': round(short_now, 2),
            'long_ma_now': round(long_now, 2),
            'details': f'MA{ma_short_period}={short_now:.2f}，MA{ma_long_period}={long_now:.2f}，差距={gap_now:+.2f}',
        }

    # ── 線性回歸預測未來收盤價 ────────────────────────────────
    # 用最近 max(20, ma_long_period) 天做線性回歸
    fit_n = max(20, ma_long_period)
    x = np.arange(fit_n)
    y = closes[-fit_n:]
    coeffs = np.polyfit(x, y, 1)   # 一次多項式
    slope_per_day, intercept = coeffs

    # 預測未來 forecast_days 天的收盤價
    future_closes = np.array([
        intercept + slope_per_day * (fit_n + d)
        for d in range(forecast_days)
    ])

    # 把歷史 + 預測拼接，重新計算均線
    extended = np.concatenate([closes, future_closes])
    ext_short = rolling_ma(extended, ma_short_period)
    ext_long  = rolling_ma(extended, ma_long_period)

    # 在未來部分尋找交叉
    hist_len = len(closes)
    cross_day = None
    cross_type = 'none'
    for d in range(1, forecast_days):
        idx_now  = hist_len + d
        idx_prev = hist_len + d - 1
        gs_now   = ext_short[idx_now]  - ext_long[idx_now]
        gs_prev  = ext_short[idx_prev] - ext_long[idx_prev]
        if np.isnan(gs_now) or np.isnan(gs_prev):
            continue
        # 交叉檢測（符號改變）
        if gs_prev > 0 and gs_now <= 0:
            cross_day  = d
            cross_type = 'death'
            break
        elif gs_prev < 0 and gs_now >= 0:
            cross_day  = d
            cross_type = 'golden'
            break

    # 信心評分
    r_sq_points = np.corrcoef(x, y)[0, 1] ** 2
    if r_sq_points > 0.85:
        confidence = 'high'
    elif r_sq_points > 0.6:
        confidence = 'medium'
    else:
        confidence = 'low'

    if cross_day is None:
        return {
            'cross_type': 'none', 'est_days': None,
            'est_date_hint': f'預測 {forecast_days} 個交易日內不會出現均線交叉',
            'current_gap': round(gap_now, 2), 'gap_trend': gap_trend,
            'confidence': confidence,
            'short_ma_now': round(short_now, 2),
            'long_ma_now': round(long_now, 2),
            'details': f'MA{ma_short_period}={short_now:.2f}，MA{ma_long_period}={long_now:.2f}，差距縮小但尚未交叉',
        }

    cross_name = '死亡交叉' if cross_type == 'death' else '黃金交叉'
    # 換算交易日 → 自然日（假設 5/7）
    calendar_days = round(cross_day * 7 / 5)
    hint = (
        f'預估約 {cross_day} 個交易日後（{calendar_days} 個自然日）出現{cross_name}，'
        f'信心度：{{"high":"高","medium":"中","low":"低"}}["{confidence}"]'
    )
    return {
        'cross_type': cross_type,
        'est_days': cross_day,
        'est_calendar_days': calendar_days,
        'est_date_hint': hint,
        'current_gap': round(gap_now, 2),
        'gap_trend': gap_trend,
        'confidence': confidence,
        'short_ma_now': round(short_now, 2),
        'long_ma_now': round(long_now, 2),
        'details': (
            f'MA{ma_short_period}={short_now:.2f}，MA{ma_long_period}={long_now:.2f}，'
            f'差距={gap_now:+.2f}，趨勢斜率={slope_per_day:+.3f}/日，'
            f'回歸 R²={r_sq_points:.2f}'
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4. 均線排列完整判斷
# ══════════════════════════════════════════════════════════════════════════════

def classify_ma_array(close: float, ind: dict) -> dict:
    """
    判斷均線排列型態：
      多頭排列   : 股價 > MA5 > MA20 > MA60
      偏多排列   : MA5 > MA20（短期多頭）
      糾結排列   : 均線交錯
      偏空排列   : MA5 < MA20
      空頭排列   : 股價 < MA5 < MA20 < MA60
    """
    ma5  = ind.get('ma5')
    ma20 = ind.get('ma20')
    ma60 = ind.get('ma60')
    ma120 = ind.get('ma120')

    available = [v for v in [ma5, ma20, ma60] if v]
    if len(available) < 2:
        return {'label': '資料不足', 'score': 0, 'color': '#94a3b8', 'detail': '均線資料不足'}

    # 各層關係
    p_above_ma5  = close > ma5  if ma5  else None
    p_above_ma20 = close > ma20 if ma20 else None
    p_above_ma60 = close > ma60 if ma60 else None
    ma5_above_20 = ma5  > ma20  if (ma5 and ma20) else None
    ma20_above_60= ma20 > ma60  if (ma20 and ma60) else None
    ma60_above_120 = ma60 > ma120 if (ma60 and ma120) else None

    # 完整多頭排列
    if (p_above_ma5 and p_above_ma20 and p_above_ma60
            and ma5_above_20 and ma20_above_60):
        label, score, color = '多頭排列', 3, '#15803d'
        detail = '股價 > MA5 > MA20 > MA60，多頭最強型態'

    # 完整空頭排列
    elif (p_above_ma5 is False and p_above_ma20 is False and p_above_ma60 is False
            and ma5_above_20 is False and ma20_above_60 is False):
        label, score, color = '空頭排列', -3, '#b91c1c'
        detail = 'MA60 > MA20 > MA5 > 股價，空頭最弱型態'

    elif ma5_above_20 and ma20_above_60:
        label, score, color = '偏多排列', 2, '#16a34a'
        detail = 'MA5 > MA20 > MA60，均線多頭排列，股價尚在整理'

    elif ma5_above_20 is False and ma20_above_60 is False:
        label, score, color = '偏空排列', -2, '#dc2626'
        detail = 'MA5 < MA20 < MA60，均線空頭排列'

    elif ma5_above_20:
        label, score, color = '短多整理', 1, '#22c55e'
        detail = 'MA5 > MA20，短期偏多，中期方向待確認'

    elif ma5_above_20 is False:
        label, score, color = '短空整理', -1, '#f97316'
        detail = 'MA5 < MA20，短期偏空，中期方向待確認'

    else:
        label, score, color = '均線糾結', 0, '#94a3b8'
        detail = '均線交錯，趨勢不明，建議觀望'

    return {
        'label': label, 'score': score, 'color': color, 'detail': detail,
        'p_above_ma5': p_above_ma5, 'p_above_ma20': p_above_ma20, 'p_above_ma60': p_above_ma60,
        'ma5_above_20': ma5_above_20, 'ma20_above_60': ma20_above_60,
        'ma60_above_120': ma60_above_120,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5. 完整均線分析入口
# ══════════════════════════════════════════════════════════════════════════════

def analyze_ma(
    close_series: list[float],
    ind: dict,
    ma_series_dict: Optional[dict] = None,
) -> dict:
    """
    一次性輸出所有均線分析結果。

    Parameters
    ----------
    close_series  : 完整收盤價序列（最新在末尾）
    ind           : 最新技術指標 dict（含 ma5/ma20/ma60 等）
    ma_series_dict: 各均線歷史陣列 dict（可選，用於葛蘭碧精確判斷）

    Returns
    -------
    dict 包含：
      bias          : 各週期乖離率
      array         : 均線排列型態
      granville     : 葛蘭碧法則觸發清單
      cross_5_20    : MA5/MA20 交叉預測
      cross_20_60   : MA20/MA60 交叉預測
      cross_5_60    : MA5/MA60 交叉預測（可選長期參考）
      summary       : 文字摘要
    """
    close = close_series[-1] if close_series else 0.0

    # 乖離率
    bias = {
        'ma5':   calc_bias(close, ind.get('ma5')),
        'ma20':  calc_bias(close, ind.get('ma20')),
        'ma60':  calc_bias(close, ind.get('ma60')),
        'ma120': calc_bias(close, ind.get('ma120')),
    }
    bias_warn = {
        'ma5':   bias_warning(bias['ma5'],   5),
        'ma20':  bias_warning(bias['ma20'],  20),
        'ma60':  bias_warning(bias['ma60'],  60),
        'ma120': bias_warning(bias['ma120'], 120),
    }

    # 均線排列
    array = classify_ma_array(close, ind)

    # 葛蘭碧
    granville_20 = []
    granville_60 = []
    if ma_series_dict and close_series:
        if ma_series_dict.get('ma20'):
            granville_20 = calc_granville_signals(close_series, ma_series_dict['ma20'])
        if ma_series_dict.get('ma60'):
            granville_60 = calc_granville_signals(close_series, ma_series_dict['ma60'])

    # 死亡/黃金交叉預測（三組）
    cross_5_20  = estimate_cross_days(close_series, 5,  20)
    cross_20_60 = estimate_cross_days(close_series, 20, 60)
    cross_5_60  = estimate_cross_days(close_series, 5,  60)

    # 文字摘要
    parts = [f'均線型態：{array["label"]}（{array["detail"]}）']
    if bias['ma20'] is not None:
        parts.append(f'MA20 乖離率：{bias["ma20"]:+.1f}%（{bias_warn["ma20"]["text"]}）')
    if cross_5_20['cross_type'] != 'none':
        parts.append(f'MA5/MA20：{cross_5_20["est_date_hint"]}')
    if cross_20_60['cross_type'] != 'none':
        parts.append(f'MA20/MA60：{cross_20_60["est_date_hint"]}')
    if granville_20:
        names = [g['name'] for g in granville_20]
        parts.append(f'葛蘭碧（MA20）：{"、".join(names)}')

    return {
        'bias': bias,
        'bias_warning': bias_warn,
        'array': array,
        'granville_20': granville_20,
        'granville_60': granville_60,
        'cross_5_20': cross_5_20,
        'cross_20_60': cross_20_60,
        'cross_5_60': cross_5_60,
        'summary': '；'.join(parts),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6. 強化版 _calc_trend()  （直接替換 app.py 中原函數）
# ══════════════════════════════════════════════════════════════════════════════

def enhanced_calc_trend(
    close: float,
    ind: dict,
    ohlcv: list[dict] | None = None,
    ma_series_dict: dict | None = None,
) -> dict:
    """
    強化版趨勢判斷，整合：
      - 原有均線/MACD/RSI 評分
      - 均線排列分類
      - 乖離率
      - 葛蘭碧法則觸發
      - 死亡/黃金交叉預測

    可直接取代 app.py 中的 _calc_trend()，回傳格式完全相容並擴充。
    """
    ma5  = ind.get('ma5')
    ma20 = ind.get('ma20')
    ma60 = ind.get('ma60')
    macd = ind.get('macd')
    rsi  = ind.get('rsi')

    score = 0
    signals = []

    # ── 均線多空評分（與原版相同）────────────────────────────
    if ma5 and ma20:
        if ma5 > ma20:
            score += 2; signals.append('MA5>MA20（短線偏多）')
        else:
            score -= 2; signals.append('MA5<MA20（短線偏空）')
    if ma20 and ma60:
        if ma20 > ma60:
            score += 2; signals.append('MA20>MA60（中線偏多）')
        else:
            score -= 2; signals.append('MA20<MA60（中線偏空）')
    if ma20:
        if close > ma20:
            score += 1; signals.append('價格站上MA20')
        else:
            score -= 1; signals.append('價格跌破MA20')
    if macd is not None:
        if macd > 0:
            score += 1; signals.append('MACD>0（多方）')
        else:
            score -= 1; signals.append('MACD<0（空方）')

    rsi_note = ''
    if rsi:
        if rsi > 70:
            score -= 1; rsi_note = f'RSI={rsi:.1f}（超買警示）'; signals.append(rsi_note)
        elif rsi < 30:
            score += 1; rsi_note = f'RSI={rsi:.1f}（超賣反彈機會）'; signals.append(rsi_note)
        else:
            signals.append(f'RSI={rsi:.1f}（中性）')

    # ── 標籤（與原版相同）────────────────────────────────────
    if score >= 4:
        label, color = '強勢上漲', '#10b981'
    elif score >= 1:
        label, color = '偏多整理', '#6ee7b7'
    elif score <= -4:
        label, color = '弱勢下跌', '#ef4444'
    elif score <= -1:
        label, color = '偏空整理', '#fca5a5'
    else:
        label, color = '盤整', '#94a3b8'

    # ── 新增：強化分析 ────────────────────────────────────────
    close_series = ([r['close'] for r in ohlcv] if ohlcv else [close])

    # 均線排列
    ma_array = classify_ma_array(close, ind)

    # 乖離率
    bias_ma20 = calc_bias(close, ma20)
    bias_ma60 = calc_bias(close, ma60)

    # 葛蘭碧（需要均線序列）
    granville_signals = []
    if ma_series_dict and close_series and len(close_series) > 5:
        if ma_series_dict.get('ma20'):
            granville_signals = calc_granville_signals(close_series, ma_series_dict['ma20'])

    # 死亡/黃金交叉預測
    cross_5_20  = estimate_cross_days(close_series, 5,  20) if len(close_series) >= 40 else {}
    cross_20_60 = estimate_cross_days(close_series, 20, 60) if len(close_series) >= 120 else {}

    return {
        # ── 原版欄位（完全相容）────────────────────
        'label':   label,
        'color':   color,
        'score':   score,
        'signals': signals,
        # ── 新增強化欄位 ────────────────────────────
        'ma_array':         ma_array,
        'bias_ma20':        bias_ma20,
        'bias_ma60':        bias_ma60,
        'bias_warn_ma20':   bias_warning(bias_ma20, 20),
        'granville':        granville_signals,
        'cross_5_20':       cross_5_20,
        'cross_20_60':      cross_20_60,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 7. 強化版 _generate_recommendation()
# ══════════════════════════════════════════════════════════════════════════════

def enhanced_generate_recommendation(
    ticker: str,
    close: float,
    ind: dict,
    trend: dict,
    chip: dict,
    info: dict,
    div_yield,
    support: float,
    resist: float,
) -> dict:
    """
    強化版投資建議，整合葛蘭碧訊號、均線排列、乖離率、交叉預測。
    回傳格式完全相容原版，並新增 ma_analysis 欄位。
    """
    rsi          = ind.get('rsi')
    macd         = ind.get('macd')
    macd_signal  = ind.get('macd_signal')
    k, d         = ind.get('k'), ind.get('d')
    ma5  = ind.get('ma5')
    ma20 = ind.get('ma20')
    ma60 = ind.get('ma60')

    reasons_buy  = []
    reasons_sell = []
    risks        = []
    tech_score   = 0

    # ── 均線排列（強化版評分）────────────────────────────────
    ma_array = trend.get('ma_array') or classify_ma_array(close, ind)
    array_score = ma_array.get('score', 0)
    # 換算到 ±2 範圍
    if array_score >= 3:
        tech_score += 2; reasons_buy.append(f'均線{ma_array["label"]}（{ma_array["detail"]}）')
    elif array_score >= 1:
        tech_score += 1; reasons_buy.append(f'均線{ma_array["label"]}')
    elif array_score <= -3:
        tech_score -= 2; reasons_sell.append(f'均線{ma_array["label"]}（{ma_array["detail"]}）')
    elif array_score <= -1:
        tech_score -= 1; reasons_sell.append(f'均線{ma_array["label"]}')

    # ── 乖離率 ──────────────────────────────────────────────
    bias_ma20 = trend.get('bias_ma20') or calc_bias(close, ma20)
    bias_warn = trend.get('bias_warn_ma20') or bias_warning(bias_ma20, 20)
    if bias_warn['level'] in ('overbought', 'extreme_overbought'):
        tech_score -= 1; risks.append(f'MA20 乖離率{bias_warn["text"]}，注意回調風險')
    elif bias_warn['level'] in ('oversold', 'extreme_oversold'):
        tech_score += 1; reasons_buy.append(f'MA20 乖離率{bias_warn["text"]}')

    # ── 葛蘭碧法則（取最新觸發）────────────────────────────
    granville = trend.get('granville', [])
    strong_buys  = [g for g in granville if g['signal'] == 'buy'  and g['strength'] == 'strong']
    strong_sells = [g for g in granville if g['signal'] == 'sell' and g['strength'] == 'strong']
    mod_buys     = [g for g in granville if g['signal'] == 'buy'  and g['strength'] == 'moderate']
    mod_sells    = [g for g in granville if g['signal'] == 'sell' and g['strength'] == 'moderate']

    for g in strong_buys:
        tech_score += 2
        reasons_buy.append(f'葛蘭碧法則①②：{g["name"]}（{g["description"]}）')
    for g in strong_sells:
        tech_score -= 2
        reasons_sell.append(f'葛蘭碧法則⑤⑥：{g["name"]}（{g["description"]}）')
    for g in mod_buys[:1]:
        tech_score += 1
        reasons_buy.append(f'葛蘭碧法則：{g["name"]}')
    for g in mod_sells[:1]:
        tech_score -= 1
        reasons_sell.append(f'葛蘭碧法則：{g["name"]}')

    # ── 死亡/黃金交叉預測 ───────────────────────────────────
    cross_5_20  = trend.get('cross_5_20', {})
    cross_20_60 = trend.get('cross_20_60', {})

    for cross, label_short in [(cross_5_20, 'MA5/MA20'), (cross_20_60, 'MA20/MA60')]:
        if not cross:
            continue
        if cross.get('cross_type') == 'death':
            days = cross.get('est_days')
            conf = cross.get('confidence', 'low')
            if days and days <= 10:
                tech_score -= 2
                risks.append(f'⚠ {label_short} 預估 {days} 交易日後出現死亡交叉（信心：{conf}）')
                reasons_sell.append(f'{label_short} 即將死亡交叉（約 {days} 個交易日）')
            elif days and days <= 20:
                tech_score -= 1
                risks.append(f'{label_short} 預估 {days} 交易日後出現死亡交叉（信心：{conf}）')
        elif cross.get('cross_type') == 'golden':
            days = cross.get('est_days')
            conf = cross.get('confidence', 'low')
            if days and days <= 10:
                tech_score += 2
                reasons_buy.append(f'⭐ {label_short} 預估 {days} 交易日後出現黃金交叉（信心：{conf}）')
            elif days and days <= 20:
                tech_score += 1
                reasons_buy.append(f'{label_short} 預估 {days} 交易日後出現黃金交叉（信心：{conf}）')

    # ── MACD ────────────────────────────────────────────────
    if macd is not None:
        if macd > 0:
            tech_score += 1; reasons_buy.append('MACD > 0（多方動能）')
        else:
            tech_score -= 1; reasons_sell.append('MACD < 0（空方動能）')
    if macd is not None and macd_signal is not None:
        if macd > macd_signal:
            tech_score += 1; reasons_buy.append('MACD 黃金交叉，動能轉強')
        else:
            tech_score -= 1; reasons_sell.append('MACD 死亡交叉，動能轉弱')

    # ── RSI ─────────────────────────────────────────────────
    if rsi is not None:
        if rsi > 70:
            tech_score -= 1; risks.append(f'RSI={rsi:.1f}（超買，短線注意壓回）')
        elif rsi < 30:
            tech_score += 1; reasons_buy.append(f'RSI={rsi:.1f}（超賣，反彈機會）')

    # ── KD ──────────────────────────────────────────────────
    if k is not None and d is not None:
        if k < 20 and d < 20:
            tech_score += 1; reasons_buy.append(f'KD 超賣區（K={k:.1f}），有反彈機會')
        elif k > 80 and d > 80:
            tech_score -= 1; reasons_sell.append(f'KD 超買區（K={k:.1f}），注意短線壓力')
            risks.append('KD 處於超買，短線漲幅受限')

    # ── 支撐/壓力位置 ────────────────────────────────────────
    price_range = resist - support
    pos_pct = round((close - support) / price_range * 100, 1) if price_range > 0 else 50
    if pos_pct < 20:
        tech_score += 1; reasons_buy.append(f'接近近期支撐（{support}），風險相對低')
    elif pos_pct > 80:
        tech_score -= 1; reasons_sell.append(f'接近近期壓力（{resist}），上漲空間受限')
        risks.append(f'股價已在近期高點附近（支撐/壓力位置：{pos_pct}%）')

    # ── 基本面 ───────────────────────────────────────────────
    fund_score = 0
    pe = info.get('pe_ratio'); pb = info.get('pb_ratio'); roe = info.get('roe')
    if pe:
        if pe < 15:
            reasons_buy.append(f'本益比 {pe:.1f}x，估值相對合理'); fund_score += 1
        elif pe > 30:
            risks.append(f'本益比 {pe:.1f}x，估值偏高'); fund_score -= 1
    if pb and pb < 1.5:
        reasons_buy.append(f'股價淨值比 {pb:.2f}x，低於1.5倍'); fund_score += 1
    if roe and roe > 0.15:
        reasons_buy.append(f'ROE {roe*100:.1f}%，獲利能力優異'); fund_score += 1
    if div_yield:
        if div_yield >= 5:
            reasons_buy.append(f'殖利率 {div_yield:.2f}%，配息豐厚（高股息）'); fund_score += 1
        elif div_yield >= 3:
            reasons_buy.append(f'殖利率 {div_yield:.2f}%，配息穩定')
        elif div_yield < 1:
            risks.append(f'殖利率 {div_yield:.2f}%，配息偏低')

    # ── 綜合評分與評級 ───────────────────────────────────────
    # 計分說明：
    #   技術面（不設上下限，直接累加）：
    #     原有 8 項各 ±1 → 最高 ±8
    #     均線排列         ±2
    #     葛蘭碧強訊號（最多2則計分）±2/則 → 最高 ±4
    #     死亡/黃金交叉    10日內 ±2，10~20日 ±1
    #     乖離率警戒       超買/超賣 ±1
    #     技術面理論上限約 ±16（不截斷）
    #   基本面：-1 ~ +4（不變）
    #   總分理論範圍約 -20 ~ +20
    #   評級門檻按技術面上限比例放大（原基準 ±8 → 新 ±16，門檻 ×2）：
    #     強力買進 ≥ 12（原 6）
    #     買進     ≥  6（原 3）
    #     持有     ≥  0（不變）
    #     減碼     ≥ -6（原 -3）
    #     賣出     < -6（原 < -3）
    total_score = tech_score + fund_score

    if total_score >= 12:
        rating, rating_color, rating_bg, rating_icon = '強力買進', '#065f46', '#d1fae5', '⬆⬆'
    elif total_score >= 6:
        rating, rating_color, rating_bg, rating_icon = '買進', '#15803d', '#dcfce7', '⬆'
    elif total_score >= 0:
        rating, rating_color, rating_bg, rating_icon = '持有', '#0369a1', '#dbeafe', '➡'
    elif total_score >= -6:
        rating, rating_color, rating_bg, rating_icon = '減碼', '#b45309', '#fef3c7', '⬇'
    else:
        rating, rating_color, rating_bg, rating_icon = '賣出', '#b91c1c', '#fee2e2', '⬇⬇'

    # ── 目標價 ───────────────────────────────────────────────
    ma_center = round(ma20 * 0.6 + ma60 * 0.4, 1) if (ma20 and ma60) else None
    if ma_center:
        if total_score >= 6:
            # 買進/強力買進：以均線中心向上估算，溢價以 total_score 標準化後計算
            norm_score = total_score / 2   # 對應原版的「每多1分+3%」
            target_price = round(ma_center * (1 + (norm_score - 2) * 0.03), 1)
            target_type, target_desc = 'upside', '上漲目標（均線+趨勢溢價）'
        elif total_score >= 0:
            target_price = ma_center
            target_type, target_desc = 'fair', '合理估值（均線中心）'
        else:
            # 減碼/賣出：同樣標準化後計算下行空間
            norm_score = total_score / 2
            downside_pct = max(norm_score * 0.025, -0.15)
            target_price = round(ma_center * (1 + downside_pct), 1)
            target_type, target_desc = 'downside', '下行風險位（均線-弱勢折價）'
    else:
        target_price, target_type, target_desc = None, 'none', ''

    return {
        # ── 原版相容欄位 ───────────────────────────────
        'rating':       rating,
        'rating_color': rating_color,
        'rating_bg':    rating_bg,
        'rating_icon':  rating_icon,
        'total_score':  total_score,
        'tech_score':   tech_score,
        'fund_score':   fund_score,
        'reasons_buy':  reasons_buy,
        'reasons_sell': reasons_sell,
        'risks':        risks,
        'target_price': target_price,
        'target_type':  target_type,
        'target_desc':  target_desc,
        'support':      support,
        'resist':       resist,
        'price_position': pos_pct,
        # ── 新增強化欄位 ───────────────────────────────
        'ma_analysis': {
            'array':        ma_array,
            'bias_ma20':    bias_ma20,
            'bias_warn':    bias_warn,
            'granville':    granville,
            'cross_5_20':   cross_5_20,
            'cross_20_60':  cross_20_60,
        },
    }
