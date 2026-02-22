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
MEAN_REV_WEIGHT = 0.35  # 均值回歸權重 (35% 往長期均值靠近)
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
        if price_df is not None:
            yp = price_df[price_df['日期'].dt.year == year]['收盤價']
            if not yp.empty:
                avg_p = float(yp.mean())
        if div_df is not None:
            yd = div_df[div_df['除息日'].dt.year == year]['股利']
            if not yd.empty:
                div_sum = float(yd.sum())
                div_cnt = len(yd)
        return avg_p, div_sum, div_cnt

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

        # 均值回歸：近期高報酬往長期均值修正
        raw_total   = w_cagr + w_div
        # 若近期報酬遠高於歷史，進一步折扣
        blended_cagr = w_cagr * (1 - MEAN_REV_WEIGHT) + (LT_RETURN - w_div) * MEAN_REV_WEIGHT
        adj_total   = blended_cagr + w_div

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

        etf_shares = {t: 0.0 for t in etf_weights}
        etf_cash   = {t: 0.0 for t in etf_weights}
        results    = []
        tracking   = {t: [] for t in etf_weights}

        prev_portfolio = 0.0

        for yr_idx, year in enumerate(range(start_year, end_year + 1)):
            yr_invested = yr_dividend = 0.0
            is_first = (yr_idx == 0)

            for t, w in etf_weights.items():
                price_df, div_df = etf_data[t]
                avg_p, div_sum, _ = self._year_data(price_df, div_df, year)

                # 價格fallback
                if avg_p <= 0:
                    avg_p = hist_stats.get(t, {}).get('last_price', 20.0)

                prev_shares = etf_shares[t]
                prev_cash   = etf_cash[t]
                prev_asset  = prev_shares * avg_p + prev_cash

                # 股息收入
                div_income = prev_shares * div_sum
                yr_dividend += div_income

                # 本年度投入
                annual_dep = monthly_investment * w * 12
                if is_first:
                    annual_dep += initial_capital * w  # 第0年部署啟動資金

                yr_invested += annual_dep

                # 可用現金 = 上期剩餘 + 投入 + 股息
                avail = prev_cash + annual_dep + div_income

                # 買入股數（採小數以簡化）
                shares_bought = avail / avg_p if avg_p > 0 else 0
                new_shares    = prev_shares + shares_bought
                etf_shares[t] = new_shares
                etf_cash[t]   = 0.0

                end_asset = new_shares * avg_p

                # ROI
                if yr_idx == 0 or (prev_asset + annual_dep) <= 0:
                    roi = 0.0
                else:
                    roi = (end_asset - prev_asset - annual_dep) / (prev_asset + annual_dep) * 100

                tracking[t].append({
                    '年份': year,
                    '資料類型': 'actual',
                    '當年度提存金':       round(annual_dep),
                    '當年度配息':         round(div_income),
                    '當年度買入股數':     round(shares_bought, 2),
                    '當年度期末累計股數': round(new_shares, 2),
                    '當年度平均股價':     round(avg_p, 2),
                    '當年度剩餘現金':     0,
                    '當年度投資報酬率':   round(roi, 2),
                    '當年度個股資產':     round(end_asset),
                    '前一年度個股資產':   round(prev_asset),
                })

            # 年末總資產
            total_end = sum(
                etf_shares[t] * max(self._year_data(etf_data[t][0], etf_data[t][1], year)[0],
                                    hist_stats.get(t, {}).get('last_price', 20.0))
                for t in etf_weights
            )

            yr_return = total_end - prev_portfolio - yr_invested if yr_idx > 0 else 0
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
        for t, w in etf_weights.items():
            lp = fp['etf_last_prices'].get(t, 20.0)
            etf_prices[t] = lp
            etf_shares[t] = (last_assets * w) / lp if lp > 0 else 0.0

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

                # 可用現金 = 投入 + 股利
                avail_cash = annual_dep + div_income

                # 買入新股數
                shares_bought = avail_cash / new_price if new_price > 0 else 0
                new_shares    = prev_shares + shares_bought
                new_asset     = new_shares * new_price

                prev_asset = prev_shares * prev_price

                yr_invested   += annual_dep
                yr_dividend   += div_income
                etf_shares[t]  = new_shares
                etf_prices[t]  = new_price

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
                    '當年度剩餘現金':     0,
                    '當年度投資報酬率':   round(roi, 2),
                    '當年度個股資產':     round(new_asset),
                    '前一年度個股資產':   round(prev_asset),
                })

            total_end  = sum(etf_shares[t] * etf_prices[t] for t in etf_weights)
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

        for sim in range(MC_SIMULATIONS):
            assets = float(initial_assets)
            annual_rets = rng.normal(mu, sigma, n_years)
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
        情境分析：從使用者輸入的「初始資本」出發，計算完整投資期。
        - 與主回測邏輯一致：股利單獨計算再投入，股價報酬只含資本增值
        - 末期資產統一顯示第 n_years 年（預設30年），方便橫向比較
        - 達成判斷延伸至 50 年，確保熊市 / 盤整有足夠時間搜尋
        """
        dv         = fp['div_yield']
        base_price = fp['price_cagr']   # 純股價年化成長（不含股利）

        scenario_defs = {
            'bull':     {'price_ret':  base_price + 0.04,
                         'name': '🐂 牛市情境',
                         'desc': f'年化+4%（總報酬 {(base_price+0.04+dv)*100:.1f}%）',
                         'color': '#10b981'},
            'base':     {'price_ret':  base_price,
                         'name': '📊 基準情境',
                         'desc': f'均值回歸調整後（總報酬 {fp["total_return"]*100:.1f}%）',
                         'color': '#3b82f6'},
            'bear':     {'price_ret':  base_price - 0.05,
                         'name': '🐻 熊市情境',
                         'desc': f'年化-5%（總報酬 {(base_price-0.05+dv)*100:.1f}%）',
                         'color': '#f59e0b'},
            'sideways': {'price_ret':  0.0,
                         'name': '↔️ 盤整情境',
                         'desc': '股息再投入，股價零成長',
                         'color': '#ef4444'},
        }

        out = {}
        for key, cfg in scenario_defs.items():
            price_ret          = cfg['price_ret']
            annual_ret_display = price_ret + dv   # 總報酬率（顯示用）

            assets        = float(initial_capital)
            annual_assets = []
            total_inv = total_div = 0.0
            finish_yr  = None
            display_assets = None   # 固定記錄第 n_years 年的資產

            for yr in range(50):              # 最多搜尋 50 年以找達成年份
                inv = monthly_investment * 12
                div = assets * dv             # 股利：以期初資產計算
                total_inv += inv
                total_div += div

                # 股利再投入 + 定期定額，再做資本增值
                assets = (assets + inv + div) * (1 + price_ret)

                thresh = inflation_target * (1 + INFLATION_RATE) ** yr

                if yr < n_years:              # 前30年加入圖表資料
                    annual_assets.append(round(assets))

                if yr == n_years - 1:         # 第30年末資產（固定比較基準）
                    display_assets = round(assets)

                if finish_yr is None and assets >= thresh:
                    finish_yr = yr + 1

            out[key] = {
                'name':            cfg['name'],
                'desc':            cfg['desc'],
                'color':           cfg['color'],
                'annual_ret_pct':  round(annual_ret_display * 100, 1),
                'finish_year':     finish_yr,
                'finish_age':      (current_age + finish_yr) if finish_yr else None,
                'forecast_assets': display_assets,   # 統一第30年末資產
                'total_invested':  round(total_inv),
                'total_dividend':  round(total_div),
                'annual_assets':   annual_assets,    # 圖表用（30年）
            }
        return out

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
        actual_end   = min(ends)

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

        # ── 找達成年份 ────────────────────────────────────────
        finish_year = finish_age = None
        for i, row in enumerate(all_rows):
            if row['資料類型'] == 'forecast' and row['年末資產'] >= row['通膨門檻']:
                finish_year = i
                finish_age  = current_age + i
                break

        # ── 統計摘要 ──────────────────────────────────────────
        actual_invested  = sum(r['年度投入'] for r in actual_rows)
        actual_dividend  = sum(r['年度股利'] for r in actual_rows)
        final_assets     = actual_rows[-1]['年末資產'] if actual_rows else initial_capital
        total_invested   = sum(r['年度投入'] for r in all_rows)
        total_dividend   = sum(r['年度股利'] for r in all_rows)
        forecast_assets  = all_rows[-1]['年末資產'] if all_rows else initial_capital

        # ── 蒙地卡羅模擬 ──────────────────────────────────────
        mc = self._monte_carlo(fp, last_assets, monthly_investment,
                               inflation_target, actual_count)

        # ── 情境分析（從初始資本出發，計算整段投資期）──────────
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
            'forecast_params':  {
                'adj_total_return': round(fp['total_return'] * 100, 2),
                'raw_total_return': round(fp['raw_total'] * 100, 2),
                'div_yield':        round(fp['div_yield'] * 100, 2),
                'annual_std':       round(fp['annual_std'] * 100, 2),
                'mean_rev_note':    mc['mean_reversion_note'],
            },
        }
