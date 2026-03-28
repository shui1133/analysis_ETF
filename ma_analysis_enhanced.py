"""
ma_analysis_enhanced.py  ── V4 強化版移動平均線分析模組
════════════════════════════════════════════════════════════
新增功能：
  1. 葛蘭碧八大法則完整判斷（標記觸發的法則編號）
  2. 死亡交叉 / 黃金交叉距離預測（估算 N 天後出現）
  3. 乖離率（BIAS）計算與警戒判斷
  4. 均線多空排列判斷（5/10/20/60 完整排列）
  5. 均線支撐壓力強度評分
  6. 四種均線買賣訊號（V4 新增）：
       訊號①：價格上破 MA（買點）—— 股價由下方向上穿越均線，量能確認
       訊號②：價格下破 MA（賣點）—— 股價由上方向下跌破均線，量能確認
       訊號③：黃金交叉（買點）—— 短均線由下往上穿越長均線，趨勢轉多
       訊號④：死亡交叉（賣點）—— 短均線由上往下穿越長均線，趨勢轉空
  7. 強化版 _calc_trend()  ── 整合上述所有分析
  8. 強化版 _generate_recommendation()  ── 納入葛蘭碧+均線訊號
  9. 評級門檻重新校準  ── 技術面上限約 ±22（含均線訊號加分）
     門檻：強力買進≥12／買進≥6／持有≥0／減碼≥-6／賣出<-6

使用方式：
  from ma_analysis_enhanced import (
      analyze_ma,
      calc_granville_signals,
      calc_ma_signals,
      format_ma_signals_summary,
      estimate_cross_days,
      calc_bias,
      enhanced_calc_trend,
      enhanced_generate_recommendation,
  )

  # 在 get_stock_analysis() 中替換原有函數：
  trend          = enhanced_calc_trend(last['close'], latest_ind, ohlcv,
                                       ma_series_dict=ma_series_dict)
  recommendation = enhanced_generate_recommendation(
                      ticker, last['close'], latest_ind, trend,
                      chip, info, div_yield, support, resist)

  # 單獨使用四種均線訊號：
  signals = calc_ma_signals(close_series, ma_series_dict, volume_series)
  print(format_ma_signals_summary(signals))
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
    警戒值依各天期均線個別設定（參考 OANDA 技術分析教學門檻）：
      MA6  (≈MA5)  : 負乖離 < -3.0%  買點；正乖離 > +3.5%  賣點
      MA12 (≈MA10) : 負乖離 < -4.5%  買點；正乖離 > +5.0%  賣點
      MA24 (≈MA20) : 負乖離 < -7.0%  買點；正乖離 > +8.0%  賣點
      MA72 (≈MA60) : 負乖離 < -11.0% 買點；正乖離 > +11.0% 賣點
    極度超買/超賣門檻 = 上述門檻 × 1.5
    """
    if bias is None:
        return {'level': 'neutral', 'text': '無資料', 'color': '#94a3b8'}

    # ── 各天期均線的個別門檻（來源：OANDA 外匯/技術分析教學整理）──
    # 格式：ma_period -> (oversold_threshold, overbought_threshold)
    # 負值=超賣買點門檻，正值=超買賣點門檻
    _THRESHOLDS: dict[int, tuple[float, float]] = {
        5:   (-3.0,   3.5),   # MA5  ≈ MA6  ：-3% / +3.5%
        6:   (-3.0,   3.5),   # MA6        ：-3% / +3.5%
        10:  (-4.5,   5.0),   # MA10 ≈ MA12：-4.5% / +5%
        12:  (-4.5,   5.0),   # MA12       ：-4.5% / +5%
        20:  (-7.0,   8.0),   # MA20 ≈ MA24：-7% / +8%
        24:  (-7.0,   8.0),   # MA24       ：-7% / +8%
        60:  (-11.0, 11.0),   # MA60 ≈ MA72：-11% / +11%
        72:  (-11.0, 11.0),   # MA72       ：-11% / +11%
        120: (-15.0, 15.0),   # MA120（外插估算）
        200: (-20.0, 20.0),   # MA200（外插估算）
    }

    # 若找不到對應天期，依線性內插估算門檻
    if ma_period in _THRESHOLDS:
        oversold, overbought = _THRESHOLDS[ma_period]
    else:
        # 以 MA20 為基準，每多 10 日 ±1% 線性外插
        base_oversold, base_overbought = -7.0, 8.0
        extra = (ma_period - 20) / 10
        oversold   = base_oversold  - extra * 1.0
        overbought = base_overbought + extra * 1.0

    # 極度超買/超賣門檻 = 門檻 × 1.5
    extreme_overbought = overbought * 1.5
    extreme_oversold   = oversold   * 1.5   # 負值 × 1.5 → 更負

    if bias > extreme_overbought:
        return {'level': 'extreme_overbought', 'text': f'極度超買（{bias:+.1f}%）', 'color': '#b91c1c'}
    elif bias > overbought:
        return {'level': 'overbought',         'text': f'超買警示（{bias:+.1f}%）',  'color': '#dc2626'}
    elif bias < extreme_oversold:
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
    lookback: int = 10,
    ma_period: int = 20,
    volume_series: list[float] | None = None,
) -> list[dict]:
    """
    嚴謹版葛蘭碧法則偵測。

    ══ 實務標記原則 ══
    不是字面符合就標，而是同時滿足下列先決條件才標：
      A. 趨勢確認：MA 斜率 + 最近 10 根 K 棒位置（60%+ 在均線上/下方）
      B. 量價佐證：突破型（①⑤）必須放量；乖離型（③⑥）須達極端門檻；
                   回測型（②⑦）位置要精確
      C. 冷卻機制：同一法則 10 根 K 棒內不重複標記

    返回觸發的法則清單，每筆包含：
      rule        : 法則編號 1~8
      signal      : 'buy' | 'sell'
      name        : 中文名稱
      description : 說明
      strength    : 'strong' | 'moderate' | 'weak'
      note        : 量價佐證說明
      index       : 觸發位置（-1 = 最新）
    """
    n_all = len(close_series)
    n_ma  = len(ma_series)
    n     = min(n_all, n_ma)
    has_vol = volume_series is not None and len(volume_series) >= n
    if n < 15:
        return []

    prices = close_series
    mas    = ma_series

    # ── 均線斜率（5 日）──────────────────────────────────────────
    def ma_slope5(i: int) -> float:
        if i < 5:
            return 0.0
        return mas[i] - mas[i - 5]

    def ma_trend_up(i: int) -> bool:
        return ma_slope5(i) > (mas[i] * 0.002) if mas[i] else False

    def ma_trend_down(i: int) -> bool:
        return ma_slope5(i) < -(mas[i] * 0.005) if mas[i] else False  # 下彎門檻更嚴格（>0.5%才算明顯下彎）

    def ma_flat(i: int) -> bool:
        # 走平：斜率絕對值 < 0.5%，或斜率已由負轉正趨勢（5日加速收斂）
        slope = ma_slope5(i)
        abs_slope = abs(slope)
        if abs_slope < (mas[i] * 0.005):
            return True
        # 斜率收斂：最近 3 日斜率比 3 日前改善 30%
        if i >= 6 and mas[i] and mas[i - 3]:
            prev_slope = mas[i - 3] - mas[i - 8] if i >= 8 and mas[i - 8] else slope
            if slope < 0 and abs(slope) < abs(prev_slope) * 0.7:
                return True  # 下彎斜率快速收斂，視為趨於走平
        return False

    # ── 趨勢環境（最近 10 根 K 棒）────────────────────────────────
    def is_bull_trend(i: int) -> bool:
        if i < 10:
            return False
        above = sum(1 for k in range(i - 10, i + 1)
                    if prices[k] is not None and mas[k] is not None and prices[k] > mas[k])
        return above >= 7 and (ma_trend_up(i) or ma_flat(i))

    def is_bear_trend(i: int) -> bool:
        if i < 10:
            return False
        below = sum(1 for k in range(i - 10, i + 1)
                    if prices[k] is not None and mas[k] is not None and prices[k] < mas[k])
        return below >= 7 and (ma_trend_down(i) or ma_flat(i))

    # ── 量能計算（近 10 日均量）────────────────────────────────────
    def avg_vol10(i: int) -> float | None:
        if not has_vol or i < 10:
            return None
        vals = [volume_series[k] for k in range(i - 10, i) if volume_series[k] and volume_series[k] > 0]
        return sum(vals) / len(vals) if vals else None

    def is_vol_boom(i: int) -> bool | None:
        avg = avg_vol10(i)
        if avg is None:
            return None
        return volume_series[i] >= avg * 1.3

    def is_vol_shrink(i: int) -> bool | None:
        avg = avg_vol10(i)
        if avg is None:
            return None
        return volume_series[i] <= avg * 0.7

    # ── 同法則冷卻（10 根）────────────────────────────────────────
    last_sig_idx: dict[int, int] = {}

    def can_mark(rule: int, i: int) -> bool:
        return (i - last_sig_idx.get(rule, -999)) >= 10

    def mark_sig(rule: int, i: int) -> None:
        last_sig_idx[rule] = i

    # ── 乖離率門檻（MA20）─────────────────────────────────────
    BIAS_SELL_MODERATE = 5.0    # ③ 普通正乖離賣出門檻
    BIAS_SELL_STRONG   = 8.0    # ③ 強力正乖離
    BIAS_BUY_MODERATE  = -5.0   # ⑥ 普通負乖離買進門檻
    BIAS_BUY_STRONG    = -8.0   # ⑥ 強力負乖離

    # ── 只計算 lookback 範圍內的最後 N 根 ────────────────────────
    start = max(10, n - lookback)
    results = []

    for i in range(start, n):
        p  = prices[i]
        m  = mas[i]
        if not p or not m:
            continue
        p1 = prices[i - 1] if i >= 1 else None
        m1 = mas[i - 1]    if i >= 1 else None
        if not p1 or not m1:
            continue

        bias      = (p - m) / m * 100
        cross_up  = p1 <= m1 and p > m
        cross_dn  = p1 >= m1 and p < m
        above     = p > m
        below     = p < m
        bull      = is_bull_trend(i)
        bear      = is_bear_trend(i)
        vol_boom  = is_vol_boom(i)
        vol_shrink= is_vol_shrink(i)

        rule, strength, note = 0, 'moderate', ''
        up_count, down_count = 0, 0
        mark_idx = i   # 預設標在當根，offset 法則會調整

        # ══════════════════════════════════════════════════════
        # ① 起漲買進
        # 條件：均線走平/上彎 + 股價由下往上突破 MA20
        #       + 前期多數 K 棒在均線下方（確認是突破而非假穿）
        # 標記：交叉當根（即時標記）
        # ══════════════════════════════════════════════════════
        if cross_up and (ma_trend_up(i) or ma_flat(i)) and can_mark(1, i):
            prev_below = sum(1 for k in range(max(0, i - 10), i)
                             if prices[k] is not None and mas[k] is not None and prices[k] < mas[k])
            prev_below_ratio = prev_below / min(10, i) if i > 0 else 0
            if prev_below_ratio >= 0.4 or not bull:
                if vol_boom is True:
                    rule, strength = 1, 'strong'
                    note = '放量突破均線，均線走平/上揚，起漲訊號可靠'
                elif vol_boom is None:
                    rule, strength = 1, 'moderate'
                    note = '股價突破均線，均線走平/上揚（量能資料不足）'
                elif vol_boom is False:
                    # 縮量突破仍標，但強度弱
                    rule, strength = 1, 'weak'
                    note = '縮量突破均線，均線走平/上揚，留意是否假突破'

        # ══════════════════════════════════════════════════════
        # ② 續漲買進（谷底回頭標）
        # 條件：多頭環境（均線上方），連跌 ≥2 根未跌破均線，
        #       「今天」是反彈第1天（谷底當天）
        # 標記：谷底當天（即時標記，反彈第1根）
        # ══════════════════════════════════════════════════════
        if not rule and bull and above and can_mark(2, i):
            # 今天是上漲（反彈第1天）
            today_up = p > p1
            if today_up:
                # 往前數連跌根數（昨天開始往前，均未跌破均線）
                down_count = 0
                for k in range(i - 1, max(i - 8, 0), -1):
                    if (prices[k] is not None and prices[k - 1] is not None
                            and mas[k] is not None
                            and prices[k] < prices[k - 1]   # 下跌
                            and prices[k] > mas[k]):         # 未跌破均線
                        down_count += 1
                    else:
                        break
                if down_count >= 2:
                    up_count = 1  # 今天是反彈第1天
                    rule, strength = 2, 'moderate'
                    note = f'多頭回測均線後反彈（連跌{down_count}根未破均線），谷底加碼時機'
                    mark_idx = i  # 標在谷底當天（反彈第1根）

        # ══════════════════════════════════════════════════════
        # ③ 初步賣出（峰值回頭標）
        # 條件：股價在均線上方，正乖離達門檻（短期漲幅已高），
        #       確認連跌 ≥3 根後，回頭標在峰值那天
        # 標記：連跌起點前一天（波段峰值）
        # ══════════════════════════════════════════════════════
        if not rule and above and can_mark(3, i):
            # 往前數連跌根數（今天往前）
            down_count = 0
            for k in range(i, max(i - 8, 0), -1):
                if prices[k] is not None and prices[k - 1] is not None and prices[k] < prices[k - 1]:
                    down_count += 1
                else:
                    break
            if down_count >= 3:
                # 峰值 = 連跌前一天
                peak_idx = i - down_count
                if peak_idx >= 0 and prices[peak_idx] is not None and mas[peak_idx] is not None:
                    peak_price = prices[peak_idx]
                    peak_ma    = mas[peak_idx]
                    peak_bias  = (peak_price - peak_ma) / peak_ma * 100
                    if peak_bias >= BIAS_SELL_MODERATE and peak_price > peak_ma:
                        strength  = 'strong' if peak_bias >= BIAS_SELL_STRONG else 'moderate'
                        note      = f'股價在均線上方達波段高點（正乖離{peak_bias:.1f}%），連跌{down_count}根確認，逢高出脫'
                        rule      = 3
                        mark_idx  = peak_idx   # ← 回頭標峰值那天
                        bias      = peak_bias   # 用峰值當天的乖離率顯示

        # ══════════════════════════════════════════════════════
        # ④ 漲勢最後買進
        # 條件：股價由均線上方跌至均線下方（cross_dn），
        #       但均線仍在上升趨勢中（多頭格局最後撐一下）
        # 標記：交叉當根（即時標記）
        # ══════════════════════════════════════════════════════
        if not rule and cross_dn and ma_trend_up(i) and can_mark(4, i):
            strong_slope = ma_slope5(i) > (m * 0.004)
            if strong_slope:
                rule, strength = 4, 'weak'
                note = '均線仍上升，股價跌破均線為漲勢中的短暫回測，最後買進機會（逆勢，小部位）'

        # ══════════════════════════════════════════════════════
        # ⑤ 趨勢轉空賣出
        # 條件：均線走平/下彎，股價由上往下跌破均線
        # 標記：交叉當根（即時標記）
        # ══════════════════════════════════════════════════════
        if not rule and cross_dn and (ma_trend_down(i) or ma_flat(i)) and can_mark(5, i):
            prev_above = sum(1 for k in range(max(0, i - 10), i)
                             if prices[k] is not None and mas[k] is not None and prices[k] > mas[k])
            prev_above_ratio = prev_above / min(10, i) if i > 0 else 0
            if prev_above_ratio >= 0.4 or not bear:
                if vol_boom is True:
                    rule, strength = 5, 'strong'
                    note = '均線走平/下彎，放量跌破均線，趨勢由多轉空，賣壓沉重'
                elif vol_boom is False:
                    rule, strength = 5, 'weak'
                    note = '均線走平/下彎，縮量跌破均線，留意是否洗盤，觀察守穩'
                else:
                    rule, strength = 5, 'moderate'
                    note = '均線走平/下彎，股價跌破均線，趨勢轉空訊號（量能資料不足）'

        # ══════════════════════════════════════════════════════
        # ⑥ 反彈買進（谷底即時標）
        # 條件：股價在均線下方，負乖離過大（嚴重超賣），
        #       今天止跌反彈（谷底第1天）
        # 標記：谷底當天（即時標記）
        # ══════════════════════════════════════════════════════
        if not rule and below and bias <= BIAS_BUY_MODERATE and can_mark(6, i):
            bouncing = p > p1
            if bouncing or bias <= BIAS_BUY_STRONG:
                strength = 'strong' if (bias <= BIAS_BUY_STRONG and bouncing) else 'moderate'
                note = (f'股價在均線下方且負乖離{bias:.1f}%，偏離均線過遠'
                        f'{"，今日止跌反彈，技術性回升機會" if bouncing else "，持續觀察是否止跌"}（謹慎）')
                rule = 6

        # ══════════════════════════════════════════════════════
        # ⑦ 續跌賣出（峰值回頭標）
        # 條件：空頭環境（均線下方），股價反彈但未超越均線，
        #       確認連跌 ≥2 根後，回頭標在反彈峰值那天
        # 標記：連跌起點前一天（反彈峰值）
        # ══════════════════════════════════════════════════════
        if not rule and bear and below and not cross_dn and can_mark(7, i):
            # 往前數連跌根數
            down_count = 0
            for k in range(i, max(i - 8, 0), -1):
                if prices[k] is not None and prices[k - 1] is not None and prices[k] < prices[k - 1]:
                    down_count += 1
                else:
                    break
            if down_count >= 2:
                # 峰值 = 連跌前一天（反彈最高點）
                peak_idx = i - down_count
                if peak_idx >= 0 and prices[peak_idx] is not None and mas[peak_idx] is not None:
                    peak_price = prices[peak_idx]
                    peak_ma    = mas[peak_idx]
                    # 確認峰值在均線下方（反彈未超過均線）
                    if peak_price < peak_ma:
                        near_ma_peak = (peak_ma - peak_price) / peak_ma * 100 < 8.0
                        # 確認峰值前有反彈（往前一天是上漲）
                        was_bounce = (peak_idx > 0 and prices[peak_idx - 1] is not None
                                      and peak_price > prices[peak_idx - 1])
                        if was_bounce:
                            strength  = 'strong' if vol_shrink is True else 'moderate'
                            note      = (f'空頭格局反彈未突破均線，連跌{down_count}根確認再度轉弱'
                                         f'{"（縮量誘多後下跌）" if vol_shrink is True else ""}')
                            rule      = 7
                            mark_idx  = peak_idx   # ← 回頭標反彈峰值

        # ══════════════════════════════════════════════════════
        # ⑧ 空頭中賣出（峰值回頭標）
        # 條件：均線下彎（空頭），股價短暫反彈至均線上方，
        #       正乖離達門檻後確認連跌 ≥2 根，回頭標峰值
        # 標記：連跌起點前一天（局部峰值）
        # ══════════════════════════════════════════════════════
        if not rule and ma_trend_down(i) and can_mark(8, i):
            # 往前數連跌根數
            down_count = 0
            for k in range(i, max(i - 8, 0), -1):
                if prices[k] is not None and prices[k - 1] is not None and prices[k] < prices[k - 1]:
                    down_count += 1
                else:
                    break
            if down_count >= 2:
                peak_idx = i - down_count
                if peak_idx >= 0 and prices[peak_idx] is not None and mas[peak_idx] is not None:
                    peak_price = prices[peak_idx]
                    peak_ma    = mas[peak_idx]
                    peak_bias  = (peak_price - peak_ma) / peak_ma * 100
                    # 峰值在均線上方且正乖離達門檻
                    if peak_price > peak_ma and peak_bias >= BIAS_SELL_MODERATE:
                        strength  = 'weak'
                        note      = (f'均線下彎空頭格局，股價短暫反彈至均線上方（正乖離{peak_bias:.1f}%）'
                                     f'，連跌{down_count}根確認回落，逢高出脫')
                        rule      = 8
                        mark_idx  = peak_idx   # ← 回頭標局部峰值
                        bias      = peak_bias

        # ── 寫入結果 ─────────────────────────────────────────
        if rule > 0:
            mark_sig(rule, mark_idx)
            _names = {
                1: ('起漲買進',   'buy',
                    '均線由下降轉為走平/上揚，股價由下往上突破均線，為多頭啟動的關鍵買進訊號'),
                2: ('續漲買進',   'buy',
                    f'多頭格局中股價在均線上方，連跌{down_count}根回測均線但未跌破，今日反彈為谷底加碼時機'),
                3: ('初步賣出',   'sell',
                    f'股價在均線上方偏離過高（正乖離{bias:.1f}%），達波段峰值後連跌{down_count}根確認轉弱，短期漲幅已高可逢高出脫'),
                4: ('漲勢最後買進', 'buy',
                    '股價由均線上方跌至均線下方，但均線仍處上升趨勢，為多頭漲勢中的最後買進機會（逆勢操作，控制部位）'),
                5: ('趨勢轉空賣出', 'sell',
                    '均線由上升轉為走平/下彎，股價由上往下跌破均線，趨勢由多轉空的關鍵賣出訊號'),
                6: ('反彈買進',   'buy',
                    f'股價在均線下方且嚴重偏離（負乖離{bias:.1f}%），今日止跌反彈，技術性回升買進機會（空頭格局需謹慎）'),
                7: ('續跌賣出',   'sell',
                    f'空頭格局中股價反彈但未突破均線，連跌{down_count}根確認再度轉弱，峰值為賣出時機'),
                8: ('空頭中賣出', 'sell',
                    f'均線下彎空頭格局中，股價短暫反彈至均線上方（正乖離{bias:.1f}%），連跌{down_count}根確認回落，局部峰值逢高出脫'),
            }
            name, signal, desc = _names[rule]
            results.append({
                'rule':        rule,
                'signal':      signal,
                'name':        name,
                'description': desc,
                'strength':    strength,
                'note':        note,
                'index':       mark_idx - n,   # offset 後的實際標記位置
                'bias':        round(bias, 2),
            })

    # 去重：同一法則保留最新觸發
    seen: set[int] = set()
    unique = []
    for r in reversed(results):
        if r['rule'] not in seen:
            seen.add(r['rule'])
            unique.append(r)
    return list(reversed(unique))


# ══════════════════════════════════════════════════════════════════════════════
# 3. 四種均線買賣訊號
# ══════════════════════════════════════════════════════════════════════════════

def calc_ma_signals(
    close_series: list[float],
    ma_series_dict: dict,
    volume_series: list[float] | None = None,
    lookback: int = 5,
) -> list[dict]:
    """
    檢測四種標準均線買賣訊號：

    訊號一：價格上破 MA（買點）
      股價從均線下方向上穿越，短期動能轉強；搭配放量更可靠。
    訊號二：價格下破 MA（賣點）
      股價從均線上方向下跌破，短期支撐失守；伴隨量能放大賣壓更重。
    訊號三：黃金交叉（買點）
      短期均線由下往上穿越長期均線，趨勢由空轉多。
    訊號四：死亡交叉（賣點）
      短期均線由上往下穿越長期均線，趨勢由多轉空。

    Parameters
    ----------
    close_series   : 完整收盤價序列（最新在末尾）
    ma_series_dict : 各均線歷史陣列，key 為 'ma5'/'ma20'/'ma60' 等
    volume_series  : 成交量序列（可選，用於量能確認）
    lookback       : 往回檢查的 K 棒數（預設 5）

    Returns
    -------
    list[dict]，每筆包含：
      signal_type : 'price_cross_up' | 'price_cross_down' |
                    'golden_cross' | 'death_cross'
      signal      : 'buy' | 'sell'
      name        : 訊號名稱（中文）
      description : 詳細說明
      ma_pair     : 涉及的均線，如 ('price','ma20') 或 ('ma5','ma20')
      strength    : 'strong' | 'moderate' | 'weak'
      volume_confirm : True/False/None（量能是否確認）
      index       : 觸發位置（-1=最新，-2=前一日 …）
    """
    results: list[dict] = []

    n_close = len(close_series)
    if n_close < 15:   # 至少 15 根才能判斷趨勢
        return results

    # ── 量能確認（近 10 日均量，門檻 1.3x）──────────────────────
    def _vol_confirm(abs_idx: int) -> bool | None:
        if not volume_series or len(volume_series) <= abs_idx or abs_idx < 10:
            return None
        vals = [volume_series[k] for k in range(abs_idx - 10, abs_idx)
                if volume_series[k] and volume_series[k] > 0]
        if not vals:
            return None
        avg = sum(vals) / len(vals)
        return volume_series[abs_idx] >= avg * 1.3   # 放量門檻提高到 1.3x

    # ── 均線斜率趨勢判斷（5 日）────────────────────────────────
    def _ma_slope(ma_arr: list, i: int) -> float:
        if i < 5 or ma_arr[i] is None or ma_arr[i - 5] is None:
            return 0.0
        return ma_arr[i] - ma_arr[i - 5]

    def _ma_up(ma_arr: list, i: int) -> bool:
        return _ma_slope(ma_arr, i) > (ma_arr[i] * 0.003) if ma_arr[i] else False

    def _ma_down(ma_arr: list, i: int) -> bool:
        return _ma_slope(ma_arr, i) < -(ma_arr[i] * 0.003) if ma_arr[i] else False

    # ── 趨勢環境判斷（最近 10 根 K 棒在均線上/下方比例）──────────
    def _bull_env(ma_arr: list, i: int) -> bool:
        """股價多數在均線上方且均線不下彎 → 多頭環境"""
        if i < 10:
            return False
        above = sum(1 for k in range(i - 10, i + 1)
                    if k < len(close_series) and k < len(ma_arr)
                    and close_series[k] is not None and ma_arr[k] is not None
                    and close_series[k] > ma_arr[k])
        return above >= 7 and not _ma_down(ma_arr, i)

    def _bear_env(ma_arr: list, i: int) -> bool:
        """股價多數在均線下方且均線不上彎 → 空頭環境"""
        if i < 10:
            return False
        below = sum(1 for k in range(i - 10, i + 1)
                    if k < len(close_series) and k < len(ma_arr)
                    and close_series[k] is not None and ma_arr[k] is not None
                    and close_series[k] < ma_arr[k])
        return below >= 7 and not _ma_up(ma_arr, i)

    # ──────────────────────────────────────────────────────────
    # 訊號① & ②：價格上破 / 下破均線（加入趨勢先決條件）
    # ──────────────────────────────────────────────────────────
    for ma_key in ('ma5', 'ma20', 'ma60'):
        ma_arr = ma_series_dict.get(ma_key)
        if not ma_arr or len(ma_arr) < 15:
            continue

        n        = min(lookback, len(close_series), len(ma_arr))
        base_idx = len(close_series) - n   # 在完整序列中的起始位置
        prices   = close_series[-n:]
        mas      = ma_arr[-n:]
        ma_label = ma_key.upper()

        for i in range(1, n):
            abs_i      = base_idx + i      # 完整序列中的絕對索引
            prev_above = prices[i - 1] > mas[i - 1]
            curr_above = prices[i]     > mas[i]
            neg_idx    = i - n

            # ── 訊號① 價格上破（買點）──────────────────────────
            if not prev_above and curr_above:
                # 先決：縮量不標（可能是假突破）
                vol_ok = _vol_confirm(abs_i)
                if vol_ok is False:
                    # 縮量上破：降為弱訊號，僅記錄不計分
                    results.append({
                        'signal_type':    'price_cross_up',
                        'signal':         'buy',
                        'name':           f'價格上破 {ma_label}',
                        'description':    f'股價向上穿越 {ma_label}，但量能不足（縮量），注意假突破風險。',
                        'ma_pair':        ('price', ma_key),
                        'strength':       'weak',
                        'volume_confirm': False,
                        'index':          neg_idx,
                    })
                else:
                    # 趨勢確認：前期應在均線下方才算有效突破（轉折意義）
                    prev_below_cnt = sum(1 for k in range(max(0, i - 10), i)
                                        if prices[k] < mas[k])
                    ratio = prev_below_cnt / min(10, i)
                    is_breakout = ratio >= 0.4   # 前期至少 40% 在均線下方
                    ma_not_falling = not _ma_down(ma_arr, abs_i)

                    if is_breakout and ma_not_falling:
                        strength = 'strong' if vol_ok is True else 'moderate'
                        vol_note = '放量突破，訊號可靠' if vol_ok is True else '量能資料不足'
                        results.append({
                            'signal_type':    'price_cross_up',
                            'signal':         'buy',
                            'name':           f'價格上破 {ma_label}',
                            'description':    (
                                f'股價由 {ma_label} 下方向上穿越（{vol_note}），'
                                f'均線{'走平/上彎' if _ma_up(ma_arr, abs_i) else '走平'}，有效突破訊號。'
                            ),
                            'ma_pair':        ('price', ma_key),
                            'strength':       strength,
                            'volume_confirm': vol_ok,
                            'index':          neg_idx,
                        })

            # ── 訊號② 價格下破（賣點）──────────────────────────
            elif prev_above and not curr_above:
                vol_ok = _vol_confirm(abs_i)
                # 先決：均線需不上彎（上升趨勢中的短暫回測不算賣點）
                ma_not_rising = not _ma_up(ma_arr, abs_i)

                prev_above_cnt = sum(1 for k in range(max(0, i - 10), i)
                                     if prices[k] > mas[k])
                ratio = prev_above_cnt / min(10, i)
                meaningful = ratio >= 0.4  # 前期至少 40% 在均線上方

                if ma_not_rising and meaningful:
                    if vol_ok is True:
                        strength = 'strong'
                        vol_note = '伴隨放量，賣壓沉重'
                    elif vol_ok is False:
                        strength = 'weak'
                        vol_note = '量能未放大，可能只是測試支撐'
                    else:
                        strength = 'moderate'
                        vol_note = '量能資料不足'
                    results.append({
                        'signal_type':    'price_cross_down',
                        'signal':         'sell',
                        'name':           f'價格下破 {ma_label}',
                        'description':    (
                            f'股價由 {ma_label} 上方向下跌破（{vol_note}），'
                            '短期支撐失守。若量能持續放大跌勢可能延續。'
                        ),
                        'ma_pair':        ('price', ma_key),
                        'strength':       strength,
                        'volume_confirm': vol_ok,
                        'index':          neg_idx,
                    })

    # ──────────────────────────────────────────────────────────
    # 訊號③ & ④：均線交叉（黃金交叉 / 死亡交叉）
    # 主要定義：MA5×MA120（短×長），且兩者都需同向
    # 輔助組合：MA5×MA20、MA20×MA60（參考用）
    # ──────────────────────────────────────────────────────────
    ma_cross_pairs = [
        ('ma5',  'ma120', '短期×長期', 'strong'),   # ★ 主要交叉：MA5×MA120
        ('ma5',  'ma20',  '短期×中期', 'moderate'),  # 輔助
        ('ma20', 'ma60',  '中期×長期', 'strong'),    # 輔助
    ]

    for short_key, long_key, pair_label, base_strength in ma_cross_pairs:
        short_arr = ma_series_dict.get(short_key)
        long_arr  = ma_series_dict.get(long_key)
        if not short_arr or not long_arr or len(short_arr) < 15 or len(long_arr) < 15:
            continue

        n     = min(lookback, len(short_arr), len(long_arr))
        # 需要額外 2 根計算方向（前 2 根斜率）
        n_ext = min(n + 2, len(short_arr), len(long_arr))
        s_mas = short_arr[-n_ext:]
        l_mas = long_arr[-n_ext:]
        short_label = short_key.upper()
        long_label  = long_key.upper()
        offset_ext = n_ext - n   # 對齊到 n 長度的偏移

        for i in range(1, n):
            i_ext = i + offset_ext   # 在 ext 陣列中的索引
            if i_ext < 2:
                continue
            prev_s_above = s_mas[i_ext - 1] > l_mas[i_ext - 1]
            curr_s_above = s_mas[i_ext]     > l_mas[i_ext]
            neg_idx      = i - n

            # 短均線方向（前2根）
            s_up   = s_mas[i_ext] > s_mas[i_ext - 2] if s_mas[i_ext - 2] is not None else s_mas[i_ext] > s_mas[i_ext - 1]
            s_down = s_mas[i_ext] < s_mas[i_ext - 2] if s_mas[i_ext - 2] is not None else s_mas[i_ext] < s_mas[i_ext - 1]

            # 黃金交叉（買點）：短均線由下往上穿越長均線，且兩者都上升
            if not prev_s_above and curr_s_above:
                abs_i   = len(long_arr) - n_ext + i_ext
                l_up    = _ma_up(long_arr, min(abs_i, len(long_arr) - 1))
                l_flat  = not _ma_down(long_arr, min(abs_i, len(long_arr) - 1))
                # ★ 嚴謹定義：黃金交叉需短均線上升 AND 長均線也上升
                both_up = s_up and l_up
                if both_up:
                    strength = base_strength
                    note = f'{short_label} 上升穿越 {long_label}，兩者均上升，黃金交叉訊號強'
                elif l_flat and s_up:
                    strength = 'moderate'
                    note = f'{short_label} 上升穿越 {long_label}，{long_label} 走平，訊號中等'
                else:
                    strength = 'weak'
                    note = f'{short_label} 穿越 {long_label}，但方向不一致，訊號偏弱'
                results.append({
                    'signal_type':    'golden_cross',
                    'signal':         'buy',
                    'name':           f'黃金交叉（{short_label}↑穿{long_label}↑）',
                    'description': (
                        f'{short_label} 由下往上穿越 {long_label}（{pair_label}），{note}。'
                        '均線為落後指標，行情通常已先走一段，進場需衡量追高風險。'
                    ),
                    'ma_pair':        (short_key, long_key),
                    'strength':       strength,
                    'volume_confirm': None,
                    'index':          neg_idx,
                    'note':           note,
                })

            # 死亡交叉（賣點）：短均線由上往下穿越長均線，且兩者都下降
            elif prev_s_above and not curr_s_above:
                abs_i   = len(long_arr) - n_ext + i_ext
                l_down  = _ma_down(long_arr, min(abs_i, len(long_arr) - 1))
                l_flat  = not _ma_up(long_arr, min(abs_i, len(long_arr) - 1))
                # ★ 嚴謹定義：死亡交叉需短均線下降 AND 長均線也下降
                both_down = s_down and l_down
                if both_down:
                    strength = base_strength
                    note = f'{short_label} 下降穿越 {long_label}，兩者均下降，死亡交叉趨勢確認'
                elif l_flat and s_down:
                    strength = 'moderate'
                    note = f'{short_label} 下降穿越 {long_label}，{long_label} 走平，訊號中等'
                else:
                    strength = 'weak'
                    note = f'{short_label} 穿越 {long_label}，但方向不一致，訊號偏弱'
                results.append({
                    'signal_type':    'death_cross',
                    'signal':         'sell',
                    'name':           f'死亡交叉（{short_label}↓穿{long_label}↓）',
                    'description': (
                        f'{short_label} 由上往下穿越 {long_label}（{pair_label}），{note}。'
                        '適合確認趨勢反轉，持股者應考慮減碼或出場。'
                    ),
                    'ma_pair':        (short_key, long_key),
                    'strength':       strength,
                    'volume_confirm': None,
                    'index':          neg_idx,
                    'note':           note,
                })

    # ── 去重：同 signal_type + ma_pair 只保留最新一筆 ──────────
    seen: set[tuple] = set()
    unique: list[dict] = []
    for r in reversed(results):
        key = (r['signal_type'], r['ma_pair'])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    unique.sort(key=lambda x: x['index'], reverse=False)
    return unique


def format_ma_signals_summary(signals: list[dict]) -> str:
    """
    將 calc_ma_signals() 結果格式化為單行摘要文字。
    例：「黃金交叉(MA5×MA20) ⬆  |  價格下破MA60 ⬇」
    """
    if not signals:
        return '無近期均線訊號'
    icon_map = {'buy': '⬆', 'sell': '⬇'}
    parts = [f'{s["name"]} {icon_map.get(s["signal"], "")}' for s in signals]
    return '  |  '.join(parts)


# ══════════════════════════════════════════════════════════════════════════════
# 4. 死亡交叉 / 黃金交叉距離預測
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
# 5. 均線排列完整判斷
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
# 6. 完整均線分析入口
# ══════════════════════════════════════════════════════════════════════════════

def analyze_ma(
    close_series: list[float],
    ind: dict,
    ma_series_dict: Optional[dict] = None,
    volume_series: list[float] | None = None,
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
            granville_20 = calc_granville_signals(close_series, ma_series_dict['ma20'],
                                                  ma_period=20, volume_series=volume_series)
        if ma_series_dict.get('ma60'):
            granville_60 = calc_granville_signals(close_series, ma_series_dict['ma60'],
                                                  ma_period=60, volume_series=volume_series)

    # 四種均線買賣訊號
    ma_signals = []
    if ma_series_dict and close_series:
        ma_signals = calc_ma_signals(close_series, ma_series_dict)

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
    if ma_signals:
        parts.append(f'均線訊號：{format_ma_signals_summary(ma_signals)}')

    return {
        'bias': bias,
        'bias_warning': bias_warn,
        'array': array,
        'granville_20': granville_20,
        'granville_60': granville_60,
        'cross_5_20': cross_5_20,
        'cross_20_60': cross_20_60,
        'cross_5_60': cross_5_60,
        'ma_signals': ma_signals,              # ← 新增四種均線買賣訊號
        'ma_signals_summary': format_ma_signals_summary(ma_signals),
        'summary': '；'.join(parts),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 7. 強化版 _calc_trend()  （直接替換 app.py 中原函數）
# ══════════════════════════════════════════════════════════════════════════════

def enhanced_calc_trend(
    close: float,
    ind: dict,
    ohlcv: list[dict] | None = None,
    ma_series_dict: dict | None = None,
    volume_series: list[float] | None = None,
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
    if not volume_series and ohlcv:
        volume_series = [r.get('volume') for r in ohlcv]

    # 均線排列
    ma_array = classify_ma_array(close, ind)

    # 乖離率
    bias_ma20 = calc_bias(close, ma20)
    bias_ma60 = calc_bias(close, ma60)

    # 葛蘭碧（需要均線序列）
    granville_signals = []
    if ma_series_dict and close_series and len(close_series) > 10:
        if ma_series_dict.get('ma20'):
            granville_signals = calc_granville_signals(
                close_series, ma_series_dict['ma20'],
                ma_period=20, volume_series=volume_series,
            )

    # 死亡/黃金交叉預測
    cross_5_20  = estimate_cross_days(close_series, 5,  20) if len(close_series) >= 40 else {}
    cross_20_60 = estimate_cross_days(close_series, 20, 60) if len(close_series) >= 120 else {}

    # 四種均線買賣訊號（需要均線序列）
    ma_signals = []
    if ma_series_dict and len(close_series) > 3:
        ma_signals = calc_ma_signals(close_series, ma_series_dict)

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
        'ma_signals':       ma_signals,               # ← 新增
        'ma_signals_summary': format_ma_signals_summary(ma_signals),   # ← 新增
    }


# ══════════════════════════════════════════════════════════════════════════════
# 8. 強化版 _generate_recommendation()
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

    # ── 四種均線買賣訊號 ─────────────────────────────────────
    # 評分規則：
    #   黃金交叉/死亡交叉（strong）： ±2
    #   黃金交叉/死亡交叉（moderate）：±1
    #   價格上破/下破（strong，放量）：±2
    #   價格上破/下破（moderate）：   ±1
    #   價格上破/下破（weak，縮量）：  ±0（只記錄，不計分）
    #   同方向訊號最多計 3 則，避免重複堆疊
    ma_signals = trend.get('ma_signals', [])
    _ma_buy_count  = 0
    _ma_sell_count = 0
    _SIGNAL_LABEL = {
        'price_cross_up':   '訊號①',
        'price_cross_down': '訊號②',
        'golden_cross':     '訊號③',
        'death_cross':      '訊號④',
    }
    for sig in ma_signals:
        stype  = sig.get('signal_type', '')
        sname  = sig.get('name', '')
        sdesc  = sig.get('description', '')
        sstr   = sig.get('strength', 'moderate')
        slabel = _SIGNAL_LABEL.get(stype, '')
        vol_ok = sig.get('volume_confirm')

        if sig['signal'] == 'buy' and _ma_buy_count < 3:
            if sstr == 'strong':
                tech_score += 2
                reasons_buy.append(f'均線{slabel}【{sname}】（{sdesc}）')
                _ma_buy_count += 1
            elif sstr == 'moderate':
                tech_score += 1
                reasons_buy.append(f'均線{slabel}【{sname}】')
                _ma_buy_count += 1
            else:  # weak（縮量，只警示不計分）
                reasons_buy.append(f'均線{slabel}【{sname}】（量能不足，謹慎）')

        elif sig['signal'] == 'sell' and _ma_sell_count < 3:
            if sstr == 'strong':
                tech_score -= 2
                reasons_sell.append(f'均線{slabel}【{sname}】（{sdesc}）')
                _ma_sell_count += 1
            elif sstr == 'moderate':
                tech_score -= 1
                reasons_sell.append(f'均線{slabel}【{sname}】')
                _ma_sell_count += 1
            else:
                reasons_sell.append(f'均線{slabel}【{sname}】（量能不足，觀察）')

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
        if pe < 12:
            reasons_buy.append(f'本益比 {pe:.1f}x，估值偏低，具安全邊際'); fund_score += 2
        elif pe < 20:
            reasons_buy.append(f'本益比 {pe:.1f}x，估值合理'); fund_score += 1
        elif pe <= 35:
            pass  # 20~35x 中性，不加不扣（科技/成長股常態）
        else:
            risks.append(f'本益比 {pe:.1f}x，估值明顯偏高'); fund_score -= 1
    # ★ 修正：P/B 依 ROE 動態判斷（高 ROE 企業合理持有高 P/B）
    if pb is not None and roe is not None:
        # 合理 P/B = ROE / 要求報酬率（預設 8%）
        fair_pb = (roe / 0.08) if roe > 0 else 1.0
        if pb < fair_pb * 0.7:
            reasons_buy.append(f'P/B {pb:.2f}x，低於合理估值（ROE {roe*100:.1f}%）'); fund_score += 1
        elif pb > fair_pb * 1.5:
            risks.append(f'P/B {pb:.2f}x，相對 ROE {roe*100:.1f}% 偏貴'); fund_score -= 1
    elif pb is not None and pb < 1.5:
        reasons_buy.append(f'股價淨值比 {pb:.2f}x，低於1.5倍'); fund_score += 1
    if roe and roe > 0.15:
        reasons_buy.append(f'ROE {roe*100:.1f}%，獲利能力優異'); fund_score += 1
    elif roe and roe > 0.08:
        reasons_buy.append(f'ROE {roe*100:.1f}%，獲利穩定')
    if div_yield:
        if div_yield >= 5:
            reasons_buy.append(f'殖利率 {div_yield:.2f}%，配息豐厚（高股息）'); fund_score += 1
        elif div_yield >= 3:
            reasons_buy.append(f'殖利率 {div_yield:.2f}%，配息穩定'); fund_score += 1
        elif div_yield >= 1:
            pass  # 1~3% 中性
        else:
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
            # ★ 修正：取 max(close, ma_center) 確保目標價一定高於現價
            norm_score = total_score / 2   # 對應原版的「每多1分+3%」
            base_up = max(close, ma_center)
            target_price = round(base_up * (1 + (norm_score - 2) * 0.03), 1)
            if target_price <= close:  # 二次保險
                target_price = round(close * 1.01, 1)
            target_type, target_desc = 'upside', '上漲目標（均線+趨勢溢價）'
        elif total_score >= 0:
            target_price = ma_center
            target_type, target_desc = 'fair', '合理估值（均線中心）'
        else:
            # 減碼/賣出：以現價或均線中心（取較低者）向下計算下行風險位
            # ★ 修正：原本用 ma_center（均線中心）計算，當股價已跌破均線時
            #   ma_center 可能遠高於現價，導致「下行風險位」反而高於現價（邏輯矛盾）
            #   改用 min(close, ma_center) 確保下行風險位一定低於現價
            norm_score = total_score / 2
            downside_pct = max(norm_score * 0.025, -0.15)   # 範圍 -0.15 ~ 0
            base_price = min(close, ma_center)               # 取現價與均線中心的較低值
            target_price = round(base_price * (1 + downside_pct), 1)
            # 二次保險：若算出來仍 >= 現價，強制改以現價為基準
            if target_price >= close:
                target_price = round(close * (1 + downside_pct), 1)
            target_type, target_desc = 'downside', '下行風險位（現價-弱勢折價）'
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
            'ma_signals':   ma_signals,                          # ← 新增
            'ma_signals_summary': format_ma_signals_summary(ma_signals),  # ← 新增
        },
    }
