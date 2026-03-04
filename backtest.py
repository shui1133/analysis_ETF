"""
Portfolio Backtest V3 with Monte Carlo Simulation
台灣ETF回測系統 V3 - 含蒙地卡羅模擬、均值回歸調整、情境分析
"""

import pandas as pd
import numpy as np
import os
import warnings
warnings.filterwarnings('ignore')

# ─────────────────────────────────────────────────────────────
# 投資組合設定
# ─────────────────────────────────────────────────────────────
PORTFOLIO_CONFIGS = {
    'conservative': {
        'name': '保守型',
        'etfs': {'00878': 0.40, '00713': 0.30, '00679B': 0.30},
        'withdrawal_rate': 0.04,
    },
    'balanced': {
        'name': '穩健型',
        'etfs': {'00919': 0.35, '00929': 0.40, '0056': 0.25},
        'withdrawal_rate': 0.06,
    },
    'aggressive': {
        'name': '積極型',
        'etfs': {'006208': 0.30, '00929': 0.50, '00915': 0.20},
        'withdrawal_rate': 0.08,
    },
}

MC_SIMULATIONS = 1000   # 蒙地卡羅模擬次數
INFLATION_RATE  = 0.03  # 通膨率假設
MEAN_REV_WEIGHT = 0.50  # 均值回歸權重 (50% 往長期均值靠近)
LT_RETURN       = 0.10  # 台灣市場長期年化報酬率假設


class PortfolioBacktestV3:

    def __init__(self, data_dir='data'):
        self.data_dir = data_dir

    # ─────────────────────────────────────────────────────────
    # 資料載入
    # ─────────────────────────────────────────────────────────
    def _load_etf_data(self, ticker):
        price_path = os.path.join(self.data_dir, f'{ticker}_price.csv')
        div_path   = os.path.join(self.data_dir, f'{ticker}_hist_配息.csv')

        price_df = div_df = None

        if os.path.exists(price_path):
            try:
                df = pd.read_csv(price_path, encoding='utf-8-sig')
                df.columns = [c.strip() for c in df.columns]
                dcol = next((c for c in df.columns if '日期' in c or c.lower() == 'date'), None)
                pcol = next((c for c in df.columns if '收盤' in c or c.lower() == 'close'), None)
                if dcol and pcol:
                    df = df[[dcol, pcol]].rename(columns={dcol: '日期', pcol: '收盤價'})
                    # 處理帶時區的日期字串
                    df['日期'] = pd.to_datetime(df['日期'], utc=True).dt.tz_localize(None)
                    df['收盤價'] = pd.to_numeric(df['收盤價'], errors='coerce')
                    df = df.dropna().sort_values('日期').reset_index(drop=True)
                    price_df = df
            except Exception as e:
                print(f'  載入 {ticker} 股價失敗: {e}')

        if os.path.exists(div_path):
            try:
                df = pd.read_csv(div_path, encoding='utf-8-sig')
                df.columns = [c.strip() for c in df.columns]
                dcol = next((c for c in df.columns if '除息' in c or '日期' in c or c.lower() == 'date'), None)
                vcol = next((c for c in df.columns if '股利' in c or c.lower() == 'dividend'), None)
                if dcol and vcol:
                    df = df[[dcol, vcol]].rename(columns={dcol: '除息日', vcol: '股利'})
                    df['除息日'] = pd.to_datetime(df['除息日'], utc=True).dt.tz_localize(None)
                    df['股利'] = pd.to_numeric(df['股利'], errors='coerce')
                    df = df.dropna().sort_values('除息日').reset_index(drop=True)
                    div_df = df
            except Exception as e:
                print(f'  載入 {ticker} 配息失敗: {e}')

        return price_df, div_df

    # ─────────────────────────────────────────────────────────
    # 歷史統計
    # ─────────────────────────────────────────────────────────
    def _calc_hist_stats(self, ticker, price_df, div_df):
        if price_df is None or price_df.empty:
            return None

        pdf = price_df.copy()
        pdf['year'] = pdf['日期'].dt.year
        annual = pdf.groupby('year')['收盤價'].last().sort_index()
        years  = list(annual.index)

        # 年度價格報酬
        price_returns = []
        for i in range(1, len(years)):
            p0, p1 = annual[years[i-1]], annual[years[i]]
            if p0 > 0:
                price_returns.append((p1 - p0) / p0)

        # CAGR
        first_p, last_p = annual.iloc[0], annual.iloc[-1]
        n = max(len(years) - 1, 1)
        cagr = (last_p / first_p) ** (1 / n) - 1 if first_p > 0 else 0

        # 近三年 CAGR (用於均值回歸判斷)
        recent_years = years[-4:] if len(years) >= 4 else years
        if len(recent_years) >= 2:
            rp0, rp1 = annual[recent_years[0]], annual[recent_years[-1]]
            rn = max(len(recent_years) - 1, 1)
            recent_cagr = (rp1 / rp0) ** (1 / rn) - 1 if rp0 > 0 else cagr
        else:
            recent_cagr = cagr

        # 配息統計
        avg_div = avg_times = div_yield = 0.0
        if div_df is not None and not div_df.empty:
            ddf = div_df.copy()
            ddf['year'] = ddf['除息日'].dt.year
            yearly = ddf.groupby('year')['股利'].agg(['sum', 'count'])
            avg_div   = float(yearly['sum'].mean())
            avg_times = float(yearly['count'].mean())
            avg_p = pdf['收盤價'].mean()
            div_yield = avg_div / avg_p if avg_p > 0 else 0.0

        avg_price = float(pdf['收盤價'].mean())
        last_price = float(last_p)

        # 年度總報酬 (價格 + 股利殖利率)
        total_returns = [r + div_yield for r in price_returns]

        return {
            'cagr':              cagr,
            'recent_cagr':       recent_cagr,
            'avg_div_per_share': avg_div,
            'avg_div_times':     avg_times,
            'avg_price':         avg_price,
            'last_price':        last_price,
            'div_yield':         div_yield,
            'price_returns':     price_returns,
            'total_returns':     total_returns,
        }

    # ─────────────────────────────────────────────────────────
    # 年度實際數據查詢
    # ─────────────────────────────────────────────────────────
    def _year_data(self, price_df, div_df, year):
        avg_p = div_sum = div_cnt = 0.0
        year_end_p = 0.0   # 年底最後收盤價，用於期末資產估值
        if price_df is not None:
            yp = price_df[price_df['日期'].dt.year == year]['收盤價']
            if not yp.empty:
                avg_p = float(yp.mean())
                year_end_p = float(yp.iloc[-1])   # 年底最後一筆收盤價
        if div_df is not None:
            yd = div_df[div_df['除息日'].dt.year == year]['股利']
            if not yd.empty:
                div_sum = float(yd.sum())
                div_cnt = len(yd)
        return avg_p, div_sum, div_cnt, year_end_p

    # ─────────────────────────────────────────────────────────
    # 均值回歸調整後預測參數
    # ─────────────────────────────────────────────────────────
    def _forecast_params(self, hist_stats, etf_weights):
        w_cagr = w_recent = w_div = w_std = 0.0
        etf_div_yields = {}
        etf_cagrs      = {}
        etf_avg_divs   = {}
        etf_last_prices = {}

        for t, w in etf_weights.items():
            if t not in hist_stats:
                continue
            s = hist_stats[t]
            w_cagr   += s['cagr'] * w
            w_recent += s['recent_cagr'] * w
            w_div    += s['div_yield'] * w
            w_std    += np.std(s['total_returns']) * w if s['total_returns'] else 0.08 * w

            etf_div_yields[t]  = s['div_yield']
            etf_cagrs[t]       = s['cagr']
            etf_avg_divs[t]    = s['avg_div_per_share']
            etf_last_prices[t] = s['last_price']

        # 均值回歸：對「總報酬」做均值回歸，往長期均值LT_RETURN靠攏
        # raw_total = 歷史股價CAGR + 配息率（歷史總報酬）
        raw_total   = w_cagr + w_div
        # 均值回歸後總報酬：50%歷史 + 50%長期均值，並設上限14%防止過度樂觀
        adj_total   = min(raw_total * (1 - MEAN_REV_WEIGHT) + LT_RETURN * MEAN_REV_WEIGHT, 0.14)
        # 純股價報酬 = 調整後總報酬 - 配息率
        blended_cagr = adj_total - w_div

        return {
            'price_cagr':    blended_cagr,
            'div_yield':     w_div,
            'total_return':  adj_total,
            'annual_std':    max(float(w_std), 0.08),
            'raw_cagr':      w_cagr,
            'raw_total':     raw_total,
            'recent_cagr':   w_recent,
            'etf_div_yields':  etf_div_yields,
            'etf_cagrs':       etf_cagrs,
            'etf_avg_divs':    etf_avg_divs,
            'etf_last_prices': etf_last_prices,
        }

    # ─────────────────────────────────────────────────────────
    # 實際回測（逐年）
    # ─────────────────────────────────────────────────────────
    def _run_actual(self, etf_weights, etf_data, hist_stats,
                    start_year, end_year,
                    initial_capital, monthly_investment, inflation_target, current_age):

        etf_shares    = {t: 0.0 for t in etf_weights}
        etf_cash      = {t: 0.0 for t in etf_weights}
        # 記錄每檔ETF「年底收盤價」，用於正確估算期末市值與下年期初資產
        etf_end_price = {t: 0.0 for t in etf_weights}
        results    = []
        tracking   = {t: [] for t in etf_weights}

        prev_portfolio = 0.0

        for yr_idx, year in enumerate(range(start_year, end_year + 1)):
            yr_invested = yr_dividend = 0.0
            is_first = (yr_idx == 0)
            etf_year_avg = {}   # 本年各ETF均價，供第0年 total_end 計算用

            for t, w in etf_weights.items():
                price_df, div_df = etf_data[t]
                avg_p, div_sum, _, year_end_p = self._year_data(price_df, div_df, year)

                # 價格 fallback
                if avg_p <= 0:
                    avg_p = hist_stats.get(t, {}).get('last_price', 20.0)
                if year_end_p <= 0:
                    year_end_p = avg_p
                etf_year_avg[t] = avg_p   # 記錄本年均價，供第0年 total_end 使用

                prev_shares = etf_shares[t]
                prev_cash   = etf_cash[t]

                # 期初資產：以「上年底收盤價」估值（第0年期初視為0）
                prev_end_p = etf_end_price[t]
                if is_first or prev_end_p <= 0:
                    prev_asset = 0.0
                else:
                    prev_asset = prev_shares * prev_end_p + prev_cash

                # 股息收入（以期初持股數 × 本年每股股利）
                div_income = prev_shares * div_sum
                yr_dividend += div_income

                # 本年度投入
                if is_first:
                    annual_dep = initial_capital * w
                else:
                    annual_dep = monthly_investment * w * 12

                yr_invested += annual_dep

                # 可用現金 = 上期剩餘 + 投入 + 股息
                avail = prev_cash + annual_dep + div_income

                # 買入股數：用「年均價」模擬定期定額分批買入的平均成本
                shares_bought = int(avail / avg_p) if avg_p > 0 else 0
                new_shares    = prev_shares + shares_bought
                etf_shares[t] = new_shares
                etf_cash[t]   = avail - shares_bought * avg_p

                # 期末資產：
                # 第0年（建倉年）：用均價（= 買入成本），確保「個股資產 = 提存金」
                # 第1年起：用年底收盤價，反映真實持倉市值
                val_price = avg_p if is_first else year_end_p
                end_asset = new_shares * val_price + etf_cash[t]

                # ROI = (期末資產 - 期初資產 - 投入) / (期初資產 + 投入)
                base = prev_asset + annual_dep
                if is_first or base <= 0:
                    roi = 0.0
                else:
                    roi = (end_asset - prev_asset - annual_dep) / base * 100

                # 記錄年底收盤價，供下一年度計算期初資產使用
                etf_end_price[t] = year_end_p

                tracking[t].append({
                    '年份': year,
                    '資料類型': 'actual',
                    '當年度提存金':       round(annual_dep),
                    '當年度配息':         round(div_income),
                    '當年度買入股數':     round(shares_bought, 2),
                    '當年度期末累計股數': round(new_shares, 2),
                    '當年度平均股價':     round(avg_p, 2),
                    '當年度年底股價':     round(year_end_p, 2),
                    '當年度剩餘現金':     round(etf_cash[t]),
                    '當年度投資報酬率':   round(roi, 2),
                    '當年度個股資產':     round(end_asset),
                    '前一年度個股資產':   round(prev_asset),
                })

            # 年末總資產：
            # 第0年（建倉年）：用均價估值（= 買入成本，年末資產 ≈ 總投入）
            # 第1年起：用年底收盤價，反映真實持倉市值
            if is_first:
                total_end = sum(etf_shares[t] * etf_year_avg[t] + etf_cash[t] for t in etf_weights)
            else:
                total_end = sum(etf_shares[t] * etf_end_price[t] + etf_cash[t] for t in etf_weights)

            yr_return = total_end - prev_portfolio - yr_invested  # 第0年：total_end - 0 - 投入 = 資本利得
            prev_portfolio = total_end

            infl_thresh = inflation_target * (INFLATION_RATE + 1) ** yr_idx

            results.append({
                '年份':     year,
                '資料類型': 'actual',
                '年度投入': round(yr_invested),
                '年度股利': round(yr_dividend),
                '年度報酬': round(yr_return),
                '年末資產': round(total_end),
                '通膨門檻': round(infl_thresh),
                '剩餘現金': 0,
            })

        return results, tracking

    # ─────────────────────────────────────────────────────────
    # 推估預測（確定性）
    # ─────────────────────────────────────────────────────────
    def _run_forecast(self, etf_weights, hist_stats, fp,
                      start_year, n_years,
                      last_assets, monthly_investment, inflation_target,
                      actual_count):

        # 以「股數」為核心單位追蹤，避免雙重計算股利
        etf_shares = {}
        etf_prices = {}
        etf_cash   = {t: 0.0 for t in etf_weights}
        for t, w in etf_weights.items():
            lp = fp['etf_last_prices'].get(t, 20.0)
            etf_prices[t] = lp
            etf_shares[t] = int((last_assets * w) / lp) if lp > 0 else 0  # 整數股數

        results    = []
        tracking   = {t: [] for t in etf_weights}

        prev_portfolio = last_assets

        for yr_idx in range(n_years):
            year = start_year + yr_idx
            yr_invested = yr_dividend = 0.0

            for t, w in etf_weights.items():
                prev_shares = etf_shares[t]
                prev_price  = etf_prices[t]

                dv     = fp['etf_div_yields'].get(t, fp['div_yield'])
                pr     = fp['etf_cagrs'].get(t, fp['price_cagr'])
                # 個股均值回歸（僅價格成長部分）
                pr_adj = pr * (1 - MEAN_REV_WEIGHT) + (LT_RETURN - dv) * MEAN_REV_WEIGHT

                # 今年股價（純價格成長，不含股利）
                new_price = prev_price * (1 + pr_adj)

                # 股利收入（以期初股數 × 每股股利）
                avg_div_per_share = fp['etf_avg_divs'].get(t, dv * prev_price)
                div_income = prev_shares * avg_div_per_share

                # 本年度定期定額投入
                annual_dep = monthly_investment * w * 12

                # 可用現金 = 上期剩餘 + 投入 + 股利
                avail_cash = etf_cash.get(t, 0.0) + annual_dep + div_income

                # 買入新股數：無條件捨去至整數，剩餘現金留至下期
                shares_bought = int(avail_cash / new_price) if new_price > 0 else 0
                remaining_cash = avail_cash - shares_bought * new_price
                new_shares    = prev_shares + shares_bought
                new_asset     = new_shares * new_price + remaining_cash

                prev_asset = prev_shares * prev_price

                yr_invested   += annual_dep
                yr_dividend   += div_income
                etf_shares[t]  = new_shares
                etf_prices[t]  = new_price
                etf_cash[t]    = remaining_cash

                # 報酬率：(期末資產 - 期初資產 - 投入) / (期初資產 + 投入)
                base = prev_asset + annual_dep
                roi  = (new_asset - prev_asset - annual_dep) / base * 100 if base > 0 else 0.0

                tracking[t].append({
                    '年份': year,
                    '資料類型': 'forecast',
                    '當年度提存金':       round(annual_dep),
                    '當年度配息':         round(div_income),
                    '當年度買入股數':     round(shares_bought, 2),
                    '當年度期末累計股數': round(new_shares, 2),
                    '當年度平均股價':     round(new_price, 2),
                    '當年度剩餘現金':     round(remaining_cash),
                    '當年度投資報酬率':   round(roi, 2),
                    '當年度個股資產':     round(new_asset),
                    '前一年度個股資產':   round(prev_asset),
                })

            total_end  = sum(etf_shares[t] * etf_prices[t] + etf_cash[t] for t in etf_weights)
            yr_return  = total_end - prev_portfolio - yr_invested
            prev_portfolio = total_end

            infl_thresh = inflation_target * (1 + INFLATION_RATE) ** (actual_count + yr_idx)

            results.append({
                '年份':     year,
                '資料類型': 'forecast',
                '年度投入': round(yr_invested),
                '年度股利': round(yr_dividend),
                '年度報酬': round(yr_return),
                '年末資產': round(total_end),
                '通膨門檻': round(infl_thresh),
                '剩餘現金': 0,
            })

        return results, tracking

    # ─────────────────────────────────────────────────────────
    # 蒙地卡羅模擬  →  P10 / P50 / P90 信心區間
    # ─────────────────────────────────────────────────────────
    def _monte_carlo(self, fp, initial_assets, monthly_investment,
                     inflation_target, actual_count, n_years=30):

        mu    = fp['total_return']
        sigma = fp['annual_std']

        rng   = np.random.default_rng(seed=42)
        paths = np.zeros((MC_SIMULATIONS, n_years))

        # 對年報酬截斷在 ±2.5σ 範圍內，避免極端離群值扭曲 P90
        effective_sigma = min(sigma, 0.15)  # sigma上限15%，避免過度波動
        clip_lo = mu - 2.5 * effective_sigma
        clip_hi = mu + 2.5 * effective_sigma

        for sim in range(MC_SIMULATIONS):
            assets = float(initial_assets)
            annual_rets = rng.normal(mu, effective_sigma, n_years)
            annual_rets = np.clip(annual_rets, clip_lo, clip_hi)
            for yr in range(n_years):
                assets = max((assets + monthly_investment * 12) * (1 + annual_rets[yr]), 0)
                paths[sim, yr] = assets

        p10 = np.percentile(paths, 10, axis=0)
        p50 = np.percentile(paths, 50, axis=0)
        p90 = np.percentile(paths, 90, axis=0)

        def first_cross(band):
            for i, v in enumerate(band):
                thresh = inflation_target * (1 + INFLATION_RATE) ** (actual_count + i)
                if v >= thresh:
                    return i + 1
            return None

        labels = [f'第{actual_count + i + 1}年' for i in range(n_years)]

        mean_reversion_note = (
            f'原始歷史年化 {fp["raw_total"]*100:.1f}%（近3年CAGR '
            f'{fp["recent_cagr"]*100:.1f}%）→ 均值回歸調整後 '
            f'{mu*100:.1f}%（σ={sigma*100:.1f}%）'
        )

        return {
            'labels':         labels,
            'p10':            [round(v) for v in p10],
            'p50':            [round(v) for v in p50],
            'p90':            [round(v) for v in p90],
            'finish_year_p10': first_cross(p10),
            'finish_year_p50': first_cross(p50),
            'finish_year_p90': first_cross(p90),
            'mu_pct':          round(mu * 100, 2),
            'sigma_pct':       round(sigma * 100, 2),
            'mean_reversion_note': mean_reversion_note,
        }

    # ─────────────────────────────────────────────────────────
    # 情境分析：牛市 / 基準 / 熊市 / 盤整
    # ─────────────────────────────────────────────────────────
    def _scenarios(self, fp, initial_capital, monthly_investment,
                   inflation_target, current_age, n_years=30):
        """
        情境分析：從初始資本出發，末期資產顯示「達成當年」的資產，
        未達成則顯示第 n_years 年末資產。搜尋上限 50 年。
        股利單獨計算再投入，股價報酬只含資本增值。
        """
        dv         = fp['div_yield']
        base_price = fp['price_cagr']

        scenario_defs = {
            'bull':     {'price_ret': base_price + 0.04,
                         'name': '🐂 牛市情境',
                         'desc': f'年化+4%（總報酬 {(base_price+0.04+dv)*100:.1f}%）',
                         'color': '#10b981'},
            'base':     {'price_ret': base_price,
                         'name': '📊 基準情境',
                         'desc': f'均值回歸調整後（總報酬 {fp["total_return"]*100:.1f}%）',
                         'color': '#3b82f6'},
            'bear':     {'price_ret': base_price - 0.05,
                         'name': '🐻 熊市情境',
                         'desc': f'年化-5%（總報酬 {(base_price-0.05+dv)*100:.1f}%）',
                         'color': '#f59e0b'},
            'sideways': {'price_ret': 0.0,
                         'name': '↔️ 盤整情境',
                         'desc': '股息再投入，股價零成長',
                         'color': '#ef4444'},
        }

        out = {}
        for key, cfg in scenario_defs.items():
            price_ret          = cfg['price_ret']
            annual_ret_display = price_ret + dv

            assets        = float(initial_capital)
            annual_assets = []
            total_inv = total_div = 0.0
            finish_yr     = None
            finish_assets = None   # 達成當年的年末資產

            for yr in range(50):
                inv = monthly_investment * 12
                div = assets * dv
                total_inv += inv
                total_div += div

                assets = (assets + inv + div) * (1 + price_ret)

                thresh = inflation_target * (1 + INFLATION_RATE) ** yr

                if yr < n_years:
                    annual_assets.append(round(assets))

                if finish_yr is None and assets >= thresh:
                    finish_yr     = yr + 1
                    finish_assets = round(assets)   # 記錄達成當年資產

            # 未達成則顯示第 n_years 年末資產
            display_assets = finish_assets if finish_assets is not None else annual_assets[-1] if annual_assets else 0

            out[key] = {
                'name':            cfg['name'],
                'desc':            cfg['desc'],
                'color':           cfg['color'],
                'annual_ret_pct':  round(annual_ret_display * 100, 1),
                'finish_year':     finish_yr,
                'finish_age':      (current_age + finish_yr) if finish_yr else None,
                'forecast_assets': display_assets,
                'total_invested':  round(total_inv),
                'total_dividend':  round(total_div),
                'annual_assets':   annual_assets,
            }
        return out

    # ─────────────────────────────────────────────────────────
    # 退休提領曲線（兩條：停止投入 / 繼續定期定額）
    # ─────────────────────────────────────────────────────────
    def _calc_retirement_series(self, all_rows, fp, finish_year,
                                withdrawal_rate, inflation_target,
                                monthly_investment, post_retire_years=30):
        """
        計算退休後兩條曲線：
        - 達到退休門檻前：與主推估一致（持續投入）
        - 達到退休門檻後：
          曲線A（停止投入）：停止定期定額，每年按提領率提領生活費，
                            剩餘資產繼續以 total_return 成長
          曲線B（繼續投入）：維持定期定額，同時每年按提領率提領生活費，
                            剩餘資產繼續以 total_return 成長

        提領金額 = 退休當年通膨門檻 × withdrawal_rate（= target_monthly_spend × 12）
        每年隨通膨率調升。
        """
        total_ret = fp['total_return']   # 年化報酬（含股利）

        # 未達成退休門檻：全部回傳 None，圖表不顯示
        if finish_year is None:
            empty = [None] * len(all_rows)
            return empty, empty

        finish_idx = finish_year - 1   # 0-based

        # ── 達成前：兩條線均沿用 all_rows 的年末資產 ──────
        pre_series = [round(all_rows[i]['年末資產']) for i in range(finish_idx + 1)]

        # 退休當年資產與初始提領金額
        retire_assets = all_rows[finish_idx]['年末資產']
        retire_thresh = all_rows[finish_idx]['通膨門檻']
        # 年提領金額 = 退休門檻 × 提領率（例：4% SWR → target_monthly_spend×12）
        annual_withdraw_base = retire_thresh * withdrawal_rate

        # ── 曲線A：停止定期定額，每年提領 ──────────────────
        series_stop = list(pre_series)
        assets_a = float(retire_assets)
        withdraw_a = annual_withdraw_base
        for _ in range(post_retire_years):
            assets_a = max(assets_a - withdraw_a, 0)   # 年初提領
            assets_a = assets_a * (1 + total_ret)       # 剩餘繼續成長
            withdraw_a *= (1 + INFLATION_RATE)           # 提領金額隨通膨調升
            series_stop.append(round(assets_a) if assets_a > 0 else 0)

        # ── 曲線B：繼續定期定額，同時每年提領 ───────────────
        series_cont = list(pre_series)
        assets_b = float(retire_assets)
        withdraw_b = annual_withdraw_base
        annual_invest = monthly_investment * 12
        for _ in range(post_retire_years):
            # 年初：先扣提領，再加入定期定額，剩餘成長
            assets_b = max(assets_b - withdraw_b, 0)
            assets_b = (assets_b + annual_invest) * (1 + total_ret)
            withdraw_b *= (1 + INFLATION_RATE)
            series_cont.append(round(assets_b) if assets_b > 0 else 0)

        return series_stop, series_cont

    # ─────────────────────────────────────────────────────────
    # 主入口
    # ─────────────────────────────────────────────────────────
    def backtest_portfolio(self, portfolio_type='conservative',
                           initial_capital=1_000_000,
                           monthly_investment=10_000,
                           current_age=30,
                           target_monthly_spend=30_000):

        cfg = PORTFOLIO_CONFIGS.get(portfolio_type)
        if not cfg:
            return None

        etf_weights     = cfg['etfs']
        withdrawal_rate = cfg['withdrawal_rate']
        portfolio_name  = cfg['name']
        inflation_target = target_monthly_spend * 12 / withdrawal_rate

        # ── 載入資料 & 歷史統計 ──────────────────────────────
        etf_data   = {}
        hist_stats = {}
        for t in etf_weights:
            pdf, ddf = self._load_etf_data(t)
            etf_data[t] = (pdf, ddf)
            s = self._calc_hist_stats(t, pdf, ddf)
            if s:
                hist_stats[t] = s

        if not hist_stats:
            return None

        # ── 實際資料年份範圍（取各ETF交集最晚起始）──────────
        starts = [p['日期'].dt.year.min() for t, (p, _) in etf_data.items() if p is not None and not p.empty]
        ends   = [p['日期'].dt.year.max() for t, (p, _) in etf_data.items() if p is not None and not p.empty]
        if not starts:
            return None

        actual_start = max(starts)
        # 排除當年度（配息尚未全數發放，避免股利顯示為0）
        import datetime
        last_complete_year = datetime.date.today().year - 1
        actual_end = min(min(ends), last_complete_year)

        # ── 實際回測 ─────────────────────────────────────────
        actual_rows, actual_track = self._run_actual(
            etf_weights, etf_data, hist_stats,
            actual_start, actual_end,
            initial_capital, monthly_investment, inflation_target, current_age,
        )

        # ── 推估參數（含均值回歸）────────────────────────────
        fp = self._forecast_params(hist_stats, etf_weights)

        # ── 確定性推估（30年）────────────────────────────────
        last_assets  = actual_rows[-1]['年末資產'] if actual_rows else initial_capital
        actual_count = len(actual_rows)  # 已有幾年實際資料

        forecast_rows, forecast_track = self._run_forecast(
            etf_weights, hist_stats, fp,
            actual_end + 1, 30,
            last_assets, monthly_investment, inflation_target, actual_count,
        )

        all_rows = actual_rows + forecast_rows

        # ── 合併 ETF 追蹤 ─────────────────────────────────────
        combined_track = {}
        for t in etf_weights:
            combined_track[t] = actual_track.get(t, []) + forecast_track.get(t, [])

        # ── 找達成年份（1-based，第幾年達成，與情境分析一致）──────
        finish_year = finish_age = None
        for i, row in enumerate(all_rows):
            if row['資料類型'] == 'forecast' and row['年末資產'] >= row['通膨門檻']:
                finish_year = i + 1        # 統一為 1-based：第1年=index0
                finish_age  = current_age + finish_year
                break

        # ── 統計摘要 ──────────────────────────────────────────
        actual_invested  = sum(r['年度投入'] for r in actual_rows)
        actual_dividend  = sum(r['年度股利'] for r in actual_rows)
        final_assets     = actual_rows[-1]['年末資產'] if actual_rows else initial_capital

        # 推估累計投入/股利：只算到達成年份（含），未達成才算全部30年
        if finish_year is not None:
            forecast_rows_to_finish = forecast_rows[:finish_year - len(actual_rows)]
        else:
            forecast_rows_to_finish = forecast_rows
        total_invested   = actual_invested + sum(r['年度投入'] for r in forecast_rows_to_finish)
        total_dividend   = actual_dividend + sum(r['年度股利'] for r in forecast_rows_to_finish)

        # 推估末期資產 = 達成當年的年末資產（非30年末）
        if finish_year is not None:
            finish_idx = finish_year - 1   # 0-based index in all_rows
            forecast_assets = all_rows[finish_idx]['年末資產']
        else:
            forecast_assets = all_rows[-1]['年末資產'] if all_rows else initial_capital

        # ── 退休提領曲線（兩條）────────────────────────────────
        # 達到退休門檻後：
        #   A. 停止定期定額，每年按提領率提領生活費
        #   B. 繼續定期定額，同時每年按提領率提領生活費
        retirement_series_stop, retirement_series_cont = self._calc_retirement_series(
            all_rows, fp, finish_year, withdrawal_rate,
            inflation_target, monthly_investment
        )

        # ── 蒙地卡羅模擬 ──────────────────────────────────────
        mc = self._monte_carlo(fp, last_assets, monthly_investment,
                               inflation_target, actual_count)

        # ── 情境分析 ──────────────────────────────────────────
        # 情境分析從初始資本出發，末期資產顯示達成當年資產
        scenarios = self._scenarios(fp, initial_capital, monthly_investment,
                                    inflation_target, current_age)

        # ── 歷史統計輸出格式 ──────────────────────────────────
        hist_stats_out = {
            t: {
                'cagr':              s['cagr'],
                'avg_div_per_share': s['avg_div_per_share'],
                'avg_div_times':     s['avg_div_times'],
                'avg_price':         s['avg_price'],
                'last_price':        s['last_price'],
            }
            for t, s in hist_stats.items()
        }

        return {
            'portfolio_name':   f'{portfolio_name}_{int(withdrawal_rate*100)}%',
            'finish_year':      finish_year,
            'finish_age':       finish_age,
            'final_assets':     final_assets,
            'actual_invested':  actual_invested,
            'actual_dividend':  actual_dividend,
            'forecast_assets':  forecast_assets,
            'total_invested':   total_invested,
            'total_dividend':   total_dividend,
            'initial_capital':  initial_capital,
            'monthly_investment': monthly_investment,
            'results':          {'annual_summary': all_rows},
            'etf_weights':      etf_weights,
            'etf_annual_tracking': combined_track,
            'hist_stats':       hist_stats_out,
            # 新增功能
            'monte_carlo':      mc,
            'scenarios':        scenarios,
            'retirement_series_stop': retirement_series_stop,
            'retirement_series_cont': retirement_series_cont,
            'forecast_params':  {
                'adj_total_return': round(fp['total_return'] * 100, 2),
                'raw_total_return': round(fp['raw_total'] * 100, 2),
                'div_yield':        round(fp['div_yield'] * 100, 2),
                'annual_std':       round(fp['annual_std'] * 100, 2),
                'mean_rev_note':    mc['mean_reversion_note'],
            },
        }
