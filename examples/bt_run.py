import backtrader as bt
import yfinance as yf
import pandas as pd
import os
import datetime
import sys # 获取当前运行脚本的路径 (in argv[0]) 
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

class RightSidePivotStrategy(bt.Strategy):
    """
    右侧交易策略：
    - 做多：寻找越来越高的低点 (Higher Lows)。当一轮小回调结束，价格重新向上拐头突破时入场。
    - 做空：寻找越来越低的高点 (Lower Highs)。当反弹结束，价格重新向下拐头跌破时入场。
    - 仓位：每次信号只使用当前总资金的 10% 进行开仓。
    - 止损：做多的止损设在最近一个波谷（低点），做空的止损设在最近一个波峰（高点）。
    """
    params = (
        ('pivot_period', 5),  # 寻找局部高低点的窗口期（左右各看几根K线）
        ('risk_percent', 0.10), # 每次开仓使用资金比例 (10%)
        ('leverage', 10.0),   # 新增：杠杆倍数，默认 10x
        ('atr_period', 14),
        ('adx_period', 14),
        ('adx_threshold', 18.0),
        ('bb_period', 20),
        ('bb_dev', 2.0),
        ('bb_bandwidth_threshold', 0.010),
        ('min_swing_atr_mult', 0.6),
        ('breakout_buffer_atr_mult', 0.10),
        ('cooldown_bars', 2),
    )

    def __init__(self):
        self.order = None
        self.buyprice = None
        self.buycomm = None
        self.stop_price = None

        # 记录资金曲线、买卖点和仓位以便绘图
        self.trade_markers = {'buy': [], 'sell': []}
        self.equity_curve = []
        self.position_curve = []
        self.position_value_curve = [] # 新增：记录仓位的名义USDT价值
        # 扩展的标记分类：建仓/加仓/平仓(盈亏)
        self.marker_entry_long = []
        self.marker_entry_short = []
        self.marker_add_long = []
        self.marker_add_short = []
        self.close_points = []  # (dt, price, pnlcomm)
        self.trade_pnls = []
        self.dt_records = []

        # 记录波峰(Highs)和波谷(Lows)
        self.highs = []
        self.lows = []
        self.last_exit_bar = -10
        self.bar_count = 0
        
        # 记录给绘图用（占位，保持兼容原有绘图代码逻辑）
        # 如果需要可以在图中绘制，这里复用 ema_fast / ema_slow 的变量名来占位以防报错
        self.ema_fast = bt.indicators.SMA(self.datas[0], period=10) 
        self.ema_slow = bt.indicators.SMA(self.datas[0], period=20)
        # 震荡/波动过滤指标（不用于趋势方向，只用于去噪）
        self.atr = bt.indicators.ATR(self.datas[0], period=self.p.atr_period)
        self.adx = bt.indicators.AverageDirectionalMovementIndex(self.datas[0], period=self.p.adx_period)
        self.bb = bt.indicators.BollingerBands(self.datas[0], period=self.p.bb_period, devfactor=self.p.bb_dev)

    def next(self):
        # 记录每根K线结束后的资金和仓位情况
        self.equity_curve.append(self.broker.getvalue())
        self.position_curve.append(self.position.size)
        
        # 记录仓位所占用的保证金成本 (Margin/Cost)
        # 即：为了持有当前这些仓位，你实际投入了多少本金
        # 如果是 10x 杠杆，名义价值是 size * price，投入本金大约是 名义价值 / 10
        # 做空时我们取绝对值，表示占用的资金量
        current_close = self.datas[0].close[0]
        pos_value = abs(self.position.size * current_close)
        margin_used = pos_value / self.p.leverage if self.position.size != 0 else 0.0
        self.position_value_curve.append(margin_used)
        
        self.dt_records.append(self.datas[0].datetime.datetime(0))
        self.bar_count += 1
        
        if self.order:
            return  # 有挂单则不处理

        # 寻找分形高低点 (Fractal Pivots)
        # 判断当前K线往前推 pivot_period 根K线，是否是局部最高或最低
        p = self.p.pivot_period
        
        # 确保有足够的数据历史
        if len(self.datas[0]) < p * 2 + 1:
            return

        # 获取前后 p 根 K 线的最高价和最低价数组
        high_slice = self.datas[0].high.get(ago=-p, size=p*2+1)
        low_slice = self.datas[0].low.get(ago=-p, size=p*2+1)
        
        if len(high_slice) != p*2+1:
            return

        center_high = high_slice[p]
        center_low = low_slice[p]

        is_pivot_high = all(center_high > h for i, h in enumerate(high_slice) if i != p)
        is_pivot_low = all(center_low < l for i, l in enumerate(low_slice) if i != p)

        # 记录最近的拐点
        if is_pivot_high:
            self.highs.append(center_high)
            # 保持列表不要太长
            if len(self.highs) > 5:
                self.highs.pop(0)
                
        if is_pivot_low:
            self.lows.append(center_low)
            if len(self.lows) > 5:
                self.lows.pop(0)

        close = self.datas[0].close[0]

        # 震荡过滤：布林带带宽过窄且 ADX 偏低，跳过
        bb_width = 0.0
        if hasattr(self.bb, 'top') and hasattr(self.bb, 'bot') and close != 0:
            bb_width = (self.bb.top[0] - self.bb.bot[0]) / abs(close)
        if not self.position and (self.bar_count - self.last_exit_bar) <= self.p.cooldown_bars:
            return
        if (bb_width < self.p.bb_bandwidth_threshold) and (self.adx[0] < self.p.adx_threshold):
            return

        # ---------------- 交易逻辑 ----------------
        if not self.position:
            # 1. 寻找做多机会：越来越高的低点 (Higher Lows) 且向上突破
            if len(self.lows) >= 2:
                # 判断最近两个低点是否抬高
                swing_ok = (self.lows[-1] - self.lows[-2]) >= self.p.min_swing_atr_mult * max(self.atr[0], 1e-9)
                if self.lows[-1] > self.lows[-2] and swing_ok:
                    # 确认右侧拐头：当前价格突破了最近的一根阻力K线（简单起见用最近两根K线的高点突破）
                    recent_high = max(self.datas[0].high[-1], self.datas[0].high[-2])
                    if close > (recent_high + self.p.breakout_buffer_atr_mult * self.atr[0]):
                        # 资金管理：计算10%资金能买多少股，并加上杠杆
                        target_value = self.broker.getvalue() * self.p.risk_percent * self.p.leverage
                        size = target_value / close
                        
                        self.order = self.buy(size=size)
                        self.stop_price = self.lows[-1] # 止损设在最近的低点拐点
                        self.log(f"做多信号(Higher Low): 价格={close:.2f}, 止损={self.stop_price:.2f}, 杠杆={self.p.leverage}x")

            # 2. 寻找做空机会：越来越低的高点 (Lower Highs) 且向下跌破
            if len(self.highs) >= 2 and not self.order:
                # 判断最近两个高点是否降低
                swing_ok = (self.highs[-2] - self.highs[-1]) >= self.p.min_swing_atr_mult * max(self.atr[0], 1e-9)
                if self.highs[-1] < self.highs[-2] and swing_ok:
                    # 确认右侧拐头：当前价格跌破最近支撑
                    recent_low = min(self.datas[0].low[-1], self.datas[0].low[-2])
                    if close < (recent_low - self.p.breakout_buffer_atr_mult * self.atr[0]):
                        target_value = self.broker.getvalue() * self.p.risk_percent * self.p.leverage
                        size = target_value / close
                        
                        self.order = self.sell(size=size)
                        self.stop_price = self.highs[-1] # 止损设在最近的高点拐点
                        self.log(f"做空信号(Lower High): 价格={close:.2f}, 止损={self.stop_price:.2f}, 杠杆={self.p.leverage}x")

        else:
            # ---------------- 止损/平仓逻辑 ----------------
            if self.position.size > 0:
                # 多头止损
                if close <= self.stop_price:
                    self.log(f"多头触及拐点止损平仓: {close:.2f} (止损价: {self.stop_price:.2f})")
                    self.order = self.close()
                # 简单止盈：如果出现了降低的高点，说明上涨动能衰竭，平多
                elif len(self.highs) >= 2 and self.highs[-1] < self.highs[-2]:
                    self.log(f"多头动能衰竭(出现Lower High)平仓: {close:.2f}")
                    self.order = self.close()
                    
            elif self.position.size < 0:
                # 空头止损
                if close >= self.stop_price:
                    self.log(f"空头触及拐点止损平仓: {close:.2f} (止损价: {self.stop_price:.2f})")
                    self.order = self.close()
                # 简单止盈：如果出现了抬高的低点，说明下跌动能衰竭，平空
                elif len(self.lows) >= 2 and self.lows[-1] > self.lows[-2]:
                    self.log(f"空头动能衰竭(出现Higher Low)平仓: {close:.2f}")
                    self.order = self.close()

    def log(self, txt, dt=None):
        dt = dt or self.datas[0].datetime.date(0)
        print(f'{dt.isoformat()}, {txt}')

    def notify_order(self, order):
        if order.status in [order.Completed]:
            dt = self.datas[0].datetime.datetime(0)
            if order.isbuy():
                self.log(f'买入成交: {order.executed.price:.2f}, 数量: {order.executed.size:.4f}')
                self.trade_markers['buy'].append((dt, order.executed.price))
                # 分类：建仓/加仓/反手
                new_size = float(self.position.size)
                exec_size = float(order.executed.size)
                prior_size = new_size - exec_size
                price = float(order.executed.price)
                if abs(prior_size) < 1e-12 and new_size > 0:
                    self.marker_entry_long.append((dt, price))
                elif prior_size > 0 and new_size > prior_size:
                    self.marker_add_long.append((dt, price))
            elif order.issell():
                self.log(f'卖出成交: {order.executed.price:.2f}, 数量: {order.executed.size:.4f}')
                self.trade_markers['sell'].append((dt, order.executed.price))
                new_size = float(self.position.size)
                exec_size = float(order.executed.size)
                prior_size = new_size - exec_size
                price = float(order.executed.price)
                if abs(prior_size) < 1e-12 and new_size < 0:
                    self.marker_entry_short.append((dt, price))
                elif prior_size < 0 and abs(new_size) > abs(prior_size):
                    self.marker_add_short.append((dt, price))
            self.buyprice = order.executed.price
            self.buycomm = order.executed.comm
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('订单取消/拒绝/保证金不足')
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f'交易结束, 毛利润: {trade.pnl:.2f}, 净利润: {trade.pnlcomm:.2f}')
            self.stop_price = None
            dt = self.datas[0].datetime.datetime(0)
            self.trade_pnls.append((dt, float(trade.pnlcomm)))
            # 记录平仓点（用于图1 Close Win/Loss）
            pr = float(self.datas[0].close[0])
            self.close_points.append((dt, pr, float(trade.pnlcomm)))
            self.last_exit_bar = self.bar_count


def plot_results(df_plot, strat, initial_cash):
    """
    封装策略回测后的可视化绘图逻辑
    """
    # ---------------- 绘图设置 ----------------
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(14, 14), sharex=True, gridspec_kw={'height_ratios': [3, 1, 1, 1]})
    fig.canvas.manager.set_window_title('Backtest Results - Vegas Tunnel')
    
    x_dt = df_plot.index
    x_num = mdates.date2num(x_dt.to_pydatetime())
    
    # 1. 价格与 EMA
    ax1.fill_between(x_dt, df_plot['ema_fast'].values, df_plot['ema_slow'].values, color='gray', alpha=0.2, label='Vegas Tunnel')
    ax1.plot(x_dt, df_plot['close'].values, '-', label='Close Price', color='#1f77b4', linewidth=1.2)
    ax1.plot(x_dt, df_plot['ema_fast'].values, '--', label='EMA(144)', color='#ff7f0e', linewidth=1.5)
    ax1.plot(x_dt, df_plot['ema_slow'].values, '--', label='EMA(169)', color='#2ca02c', linewidth=1.5)
    
    # 绘制买卖点（区分建仓/加仓/平仓）
    buys_dt = [m[0] for m in strat.trade_markers['buy']]
    buys_p = [m[1] for m in strat.trade_markers['buy']]
    sells_dt = [m[0] for m in strat.trade_markers['sell']]
    sells_p = [m[1] for m in strat.trade_markers['sell']]
    
    if hasattr(strat, 'marker_entry_long') and strat.marker_entry_long:
        e_dt = [d for d, _ in strat.marker_entry_long]
        e_p = [p for _, p in strat.marker_entry_long]
        ax1.scatter(e_dt, e_p, marker='^', color='red', s=120, label='Entry Long', zorder=6)
    if hasattr(strat, 'marker_entry_short') and strat.marker_entry_short:
        e_dt = [d for d, _ in strat.marker_entry_short]
        e_p = [p for _, p in strat.marker_entry_short]
        ax1.scatter(e_dt, e_p, marker='v', color='blue', s=120, label='Entry Short', zorder=6)
    if hasattr(strat, 'marker_add_long') and strat.marker_add_long:
        a_dt = [d for d, _ in strat.marker_add_long]
        a_p = [p for _, p in strat.marker_add_long]
        ax1.scatter(a_dt, a_p, marker='D', color='#ff7f0e', s=70, label='Add Long', zorder=6)
    if hasattr(strat, 'marker_add_short') and strat.marker_add_short:
        a_dt = [d for d, _ in strat.marker_add_short]
        a_p = [p for _, p in strat.marker_add_short]
        ax1.scatter(a_dt, a_p, marker='D', color='#17becf', s=70, label='Add Short', zorder=6)
    if hasattr(strat, 'close_points') and strat.close_points:
        c_pos_dt = [d for d, _, pnl in strat.close_points if pnl >= 0]
        c_pos_p = [p for d, p, pnl in strat.close_points if pnl >= 0]
        c_neg_dt = [d for d, _, pnl in strat.close_points if pnl < 0]
        c_neg_p = [p for d, p, pnl in strat.close_points if pnl < 0]
        if c_pos_dt:
            ax1.scatter(c_pos_dt, c_pos_p, marker='x', color='green', s=140, linewidths=2.2, label='Close (Win)', zorder=7)
        if c_neg_dt:
            ax1.scatter(c_neg_dt, c_neg_p, marker='x', color='red', s=140, linewidths=2.2, label='Close (Loss)', zorder=7)
    # 回退：若老的 buy/sell 存在，作为补充（以兼容早期数据）
    if buys_dt and not getattr(strat, 'marker_entry_long', None):
        ax1.scatter(buys_dt, buys_p, marker='^', color='red', s=120, label='Buy', zorder=5)
    if sells_dt and not getattr(strat, 'marker_entry_short', None):
        ax1.scatter(sells_dt, sells_p, marker='v', color='green', s=120, label='Sell', zorder=5)
        
    ax1.set_title('BTC-USD Trading Strategy', fontsize=14, fontweight='bold')
    ax1.set_ylabel('Price (USD)', fontsize=12)
    ax1.legend(loc='upper left', fontsize=8, frameon=True, framealpha=0.8, borderpad=0.3, labelspacing=0.3, handlelength=1.5)
    ax1.grid(True, alpha=0.3)
    # 调整Y轴范围避免从0开始
    y_stack = np.vstack([df_plot['close'].values, df_plot['ema_fast'].values, df_plot['ema_slow'].values])
    y_min = float(np.nanmin(y_stack))
    y_max = float(np.nanmax(y_stack))
    pad = (y_max - y_min) * 0.02 if y_max > y_min else 1.0
    ax1.set_ylim(y_min - pad, y_max + pad)
    
    # 2. 资金曲线 (Equity)
    ax2.plot(strat.dt_records, strat.equity_curve, '-', color='purple', linewidth=1.5, label='Portfolio Value')
    
    # 盈亏分色填充
    ax2.fill_between(strat.dt_records, strat.equity_curve, initial_cash, 
                     where=(np.array(strat.equity_curve) >= initial_cash), color='red', alpha=0.2, interpolate=True)
    ax2.fill_between(strat.dt_records, strat.equity_curve, initial_cash, 
                     where=(np.array(strat.equity_curve) < initial_cash), color='green', alpha=0.2, interpolate=True)
    
    ax2.set_ylabel('Equity', fontsize=12)
    ax2.legend(loc='upper left', fontsize=8, frameon=True, framealpha=0.8, borderpad=0.3, labelspacing=0.3, handlelength=1.5)
    ax2.grid(True, alpha=0.3)
    
    # 3. 仓位价值曲线 (Position Value in USDT)
    ax3.plot(strat.dt_records, strat.position_value_curve, '-', color='#17becf', linewidth=1.5, label='Margin Used (USDT)')
    ax3.fill_between(strat.dt_records, strat.position_value_curve, 0, alpha=0.3, color='#17becf')
    ax3.axhline(y=0, color='black', linewidth=1.0, alpha=0.5)
    ax3.set_ylabel('Margin (USDT)', fontsize=12)
    ax3.legend(loc='upper left', fontsize=8, frameon=True, framealpha=0.8, borderpad=0.3, labelspacing=0.3, handlelength=1.5)
    ax3.grid(True, alpha=0.3)
    
    # 4. 每笔平仓后的总资金变化柱状图 (Trade PnL)
    tp_dates = [pd.Timestamp(d) for d, _ in strat.trade_pnls] if hasattr(strat, 'trade_pnls') else []
    tp_values = [float(p) for _, p in strat.trade_pnls] if hasattr(strat, 'trade_pnls') else []
    colors = ['green' if val >= 0 else 'red' for val in tp_values]
    bars = ax4.bar(tp_dates, tp_values, color=colors, width=0.20, alpha=0.8, label='Trade PnL (USDT)')
    def _fmt_pnl(v):
        s = v
        sign = '+' if s >= 0 else ''
        if abs(s) >= 1e6:
            return f'{sign}{s/1e6:.1f}M'
        if abs(s) >= 1e3:
            return f'{sign}{s/1e3:.1f}k'
        return f'{sign}{s:.0f}'
    for x, y, b in zip(tp_dates, tp_values, bars):
        ax4.annotate(_fmt_pnl(y), xy=(x, y), xytext=(0, 6 if y >= 0 else -12), textcoords='offset points',
                     ha='center', va='bottom' if y >= 0 else 'top', fontsize=8,
                     color=('green' if y >= 0 else 'red'),
                     bbox=dict(boxstyle='round,pad=0.2', fc='white', alpha=0.8, edgecolor=('green' if y >= 0 else 'red')))
    ax4.axhline(y=0, color='black', linewidth=1.0, alpha=0.5)
    
    ax4.set_ylabel('Trade PnL (USDT)', fontsize=12)
    ax4.set_xlabel('Time', fontsize=12)
    ax4.legend(loc='upper left', fontsize=8, frameon=True, framealpha=0.8, borderpad=0.3, labelspacing=0.3, handlelength=1.5)
    ax4.grid(True, alpha=0.3)
    
    # 时间轴格式化
    locator = mdates.AutoDateLocator(minticks=5, maxticks=8)
    formatter = mdates.ConciseDateFormatter(locator)
    ax4.xaxis.set_major_locator(locator)
    ax4.xaxis.set_major_formatter(formatter)
    ax4.set_xlim(x_dt.min(), x_dt.max())
    
    # ---------------- 交互注释与十字线 ----------------
    ann_kw = dict(xy=(0, 0), xytext=(15, 15), textcoords='offset points',
                  bbox=dict(boxstyle='round,pad=0.5', fc='white', alpha=0.9, edgecolor='gray'),
                  arrowprops=dict(arrowstyle='->', alpha=0.5))
    ann1 = ax1.annotate('', **ann_kw)
    ann2 = ax2.annotate('', **ann_kw)
    ann3 = ax3.annotate('', **ann_kw)
    ann4 = ax4.annotate('', **ann_kw)
    p_ann1 = ax1.annotate('', **ann_kw)
    p_ann2 = ax2.annotate('', **ann_kw)
    p_ann3 = ax3.annotate('', **ann_kw)
    p_ann4 = ax4.annotate('', **ann_kw)
    
    for ann in [ann1, ann2, ann3, ann4, p_ann1, p_ann2, p_ann3, p_ann4]:
        ann.set_visible(False)
    
    vline1 = ax1.axvline(x=x_dt[0], color='black', alpha=0.4, linestyle='--')
    vline2 = ax2.axvline(x=x_dt[0], color='black', alpha=0.4, linestyle='--')
    vline3 = ax3.axvline(x=x_dt[0], color='black', alpha=0.4, linestyle='--')
    vline4 = ax4.axvline(x=x_dt[0], color='black', alpha=0.4, linestyle='--')
    
    hline1 = ax1.axhline(y=0, color='black', alpha=0.4, linestyle='--')
    hline2 = ax2.axhline(y=0, color='black', alpha=0.4, linestyle='--')
    hline3 = ax3.axhline(y=0, color='black', alpha=0.4, linestyle='--')
    hline4 = ax4.axhline(y=0, color='black', alpha=0.4, linestyle='--')
    
    vlines = [vline1, vline2, vline3, vline4]
    hlines = [hline1, hline2, hline3, hline4]
    
    p_dot1, = ax1.plot([], [], 'o', color='#1f77b4', markersize=6, alpha=0.9, zorder=6)
    p_dot2, = ax2.plot([], [], 'o', color='purple', markersize=6, alpha=0.9, zorder=6)
    p_dot3, = ax3.plot([], [], 'o', color='#17becf', markersize=6, alpha=0.9, zorder=6)
    p_dot4, = ax4.plot([], [], 'o', color='gray', markersize=6, alpha=0.9, zorder=6)
    p_dot1.set_visible(False)
    p_dot2.set_visible(False)
    p_dot3.set_visible(False)
    p_dot4.set_visible(False)
    
    for line in vlines + hlines:
        line.set_visible(False)
    
    def on_move(event):
        if not event.inaxes:
            for item in [ann1, ann2, ann3, ann4] + vlines + hlines:
                item.set_visible(False)
            fig.canvas.draw_idle()
            return
            
        mx = event.xdata
        if mx is None:
            return
            
        idx = int(np.searchsorted(x_num, mx))
        idx = max(0, min(idx, len(x_num) - 1))
        xi = x_num[idx]
        
        y_close = float(df_plot['close'].iat[idx])
        # 不使用EMA进行显示
        
        # 匹配对应时间点的资金和仓位价值 (如果有)
        eq_idx = min(idx, len(strat.equity_curve) - 1)
        y_eq = float(strat.equity_curve[eq_idx]) if eq_idx >= 0 else initial_cash
        y_pos = float(strat.position_value_curve[eq_idx]) if eq_idx >= 0 else 0.0
        
        # 匹配对应时间点的交易收益
        tp_dates_num = mdates.date2num(tp_dates) if 'tp_dates' in locals() and tp_dates else []
        if len(tp_dates_num) > 0:
            tp_idx = int(np.searchsorted(tp_dates_num, mx))
            tp_idx = max(0, min(tp_idx, len(tp_dates_num) - 1))
            y_tp = tp_values[tp_idx]
        else:
            y_tp = 0.0
            
        # 隐藏所有横线和注释
        for h in hlines: h.set_visible(False)
        for a in [ann1, ann2, ann3, ann4]: a.set_visible(False)
            
        if event.inaxes == ax1:
            ann1.xy = (xi, y_close)
            ann1.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nClose: {y_close:.2f}")
            ann1.set_visible(True)
            hline1.set_ydata([y_close, y_close])
            hline1.set_visible(True)
        elif event.inaxes == ax2:
            ann2.xy = (xi, y_eq)
            ann2.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nEquity: {y_eq:.2f}")
            ann2.set_visible(True)
            hline2.set_ydata([y_eq, y_eq])
            hline2.set_visible(True)
        elif event.inaxes == ax3:
            ann3.xy = (xi, y_pos)
            ann3.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nMargin Used: {y_pos:,.2f} USDT")
            ann3.set_visible(True)
            hline3.set_ydata([y_pos, y_pos])
            hline3.set_visible(True)
        elif event.inaxes == ax4:
            ann4.xy = (xi, y_tp)
            ann4.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nTrade PnL: {y_tp:,.2f} USDT")
            ann4.set_visible(True)
            hline4.set_ydata([y_tp, y_tp])
            hline4.set_visible(True)
            
        for v in vlines:
            v.set_xdata([xi, xi])
            v.set_visible(True)
            
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect('motion_notify_event', on_move)
    def on_click(event):
        if not event.inaxes:
            return
        mx = event.xdata
        if mx is None:
            return
        idx = int(np.searchsorted(x_num, mx))
        idx = max(0, min(idx, len(x_num) - 1))
        xi = x_num[idx]
        if event.inaxes == ax1:
            y_close = float(df_plot['close'].iat[idx])
            p_ann1.xy = (xi, y_close)
            p_ann1.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nClose: {y_close:.2f}")
            p_ann1.set_visible(True)
            p_dot1.set_data([x_dt[idx]], [y_close])
            p_dot1.set_visible(True)
        elif event.inaxes == ax2:
            eq_idx = min(idx, len(strat.equity_curve) - 1)
            y_eq = float(strat.equity_curve[eq_idx]) if eq_idx >= 0 else initial_cash
            p_ann2.xy = (xi, y_eq)
            p_ann2.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nEquity: {y_eq:.2f}")
            p_ann2.set_visible(True)
            p_dot2.set_data([x_dt[idx]], [y_eq])
            p_dot2.set_visible(True)
        elif event.inaxes == ax3:
            eq_idx = min(idx, len(strat.position_value_curve) - 1)
            y_pos = float(strat.position_value_curve[eq_idx]) if eq_idx >= 0 else 0.0
            p_ann3.xy = (xi, y_pos)
            p_ann3.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nMargin Used: {y_pos:,.2f} USDT")
            p_ann3.set_visible(True)
            p_dot3.set_data([x_dt[idx]], [y_pos])
            p_dot3.set_visible(True)
        elif event.inaxes == ax4:
            if 'tp_dates' in locals() and tp_dates:
                tp_dates_num = mdates.date2num(tp_dates)
                tp_idx = int(np.searchsorted(tp_dates_num, mx))
                tp_idx = max(0, min(tp_idx, len(tp_dates_num) - 1))
                y_tp = tp_values[tp_idx]
            else:
                y_tp = 0.0
            p_ann4.xy = (xi, y_tp)
            p_ann4.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nTrade PnL: {y_tp:,.2f} USDT")
            p_ann4.set_visible(True)
            p_dot4.set_data([x_dt[idx]], [y_tp])
            p_dot4.set_visible(True)
        fig.canvas.draw_idle()
    fig.canvas.mpl_connect('button_press_event', on_click)
    plt.tight_layout()
    out_dir = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'output')
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    out_file = os.path.join(out_dir, f"backtest_{x_dt.min().strftime('%Y%m%d')}_{x_dt.max().strftime('%Y%m%d')}_{ts}.png")
    fig.savefig(out_file, dpi=150)
    plt.show()

class CryptoCommissionInfo(bt.CommissionInfo):
    params = (
        ('commission', 0.0008), # 0.08% 佣金
        ('mult', 1.0),
        ('margin', None),      # 使用杠杆时需要设置保证金比例，或通过下面覆盖来模拟
        ('commtype', bt.CommInfoBase.COMM_PERC),
        ('stocklike', False),
        ('leverage', 10.0),    # 允许 10 倍杠杆
    )

    def getsize(self, price, cash):
        """覆盖该方法使得 broker 认为资金足够"""
        return (cash * self.p.leverage) / price

def my_strage():
    print("开始回测...")
    # 创建Cerebro引擎  
    cerebro = bt.Cerebro() 

    # 替换为新的右侧拐点策略
    cerebro.addstrategy(RightSidePivotStrategy)
    # 获取当前运行脚本所在目录  
    modpath = os.path.dirname(os.path.abspath(sys.argv[0]))

    # 用pandas读取CSV数据
    df = pd.read_csv(os.path.join(modpath, '../BTC-USD_1H_20251111_221506.csv'), parse_dates=['datetime'])
    print("数据长度：", len(df))  # df 是你的 DataFrame
    df.set_index('datetime', inplace=True)
    df.sort_index(inplace=True)
    start_dt = df.index.min()
    end_dt = df.index.max()
    df_plot = df.copy()
    df_plot['ema_fast'] = df_plot['close'].ewm(span=144, adjust=False).mean()
    df_plot['ema_slow'] = df_plot['close'].ewm(span=169, adjust=False).mean()
    data = bt.feeds.PandasData(
        dataname=df,
        timeframe=bt.TimeFrame.Minutes,   # 明确指定为分钟级别
        compression=60,                   # 1小时K线
        fromdate=start_dt.to_pydatetime(),
        todate=end_dt.to_pydatetime()
    )
    cerebro.adddata(data)
    # 设置投资金额100000.0 
    cerebro.broker.setcash(1000000.0) 
    
    # 因为加了 10x 杠杆，需要确保券商允许使用保证金(margin)交易，避免现金不足被拒绝
    comminfo = CryptoCommissionInfo()
    cerebro.broker.addcommissioninfo(comminfo)
    cerebro.broker.set_checksubmit(False) # 允许不检查现金是否足够（模拟杠杆借贷）
    
    # 新增：添加收益率分析器
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='timereturn')
    
    # 引擎运行前打印期出资金  
    initial_cash = cerebro.broker.getvalue()
    print('组合期初资金: %.2f' % initial_cash) 
    results = cerebro.run() 
    strat = results[0]
    # 引擎运行后打期末资金  
    print('组合期末资金: %.2f' % cerebro.broker.getvalue())
    
    # 调用绘图函数
    plot_results(df_plot, strat, initial_cash=initial_cash)

if __name__ == "__main__":
    my_strage()
