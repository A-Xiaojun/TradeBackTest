import backtrader as bt
import yfinance as yf
import pandas as pd
import os
import datetime
import sys # 获取当前运行脚本的路径 (in argv[0]) 
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

import backtrader as bt

class VegasTunnelStrategy(bt.Strategy):
    params = (
        ('ema_fast', 144),
        ('ema_slow', 169),
        ('stop_loss', 0.01),   # 止损百分比（如1%）
        ('take_profit', 0.02), # 止盈百分比（如2%）
        ('size', 1),           # 每次交易数量
    )

    def __init__(self):
        self.ema12 = bt.indicators.ExponentialMovingAverage(self.datas[0], period=12)
        self.ema_fast = bt.indicators.ExponentialMovingAverage(self.datas[0], period=self.p.ema_fast)
        self.ema_slow = bt.indicators.ExponentialMovingAverage(self.datas[0], period=self.p.ema_slow)
        self.order = None
        self.buyprice = None
        self.buycomm = None
        
        # 新增：记录资金曲线、买卖点和仓位以便绘图
        self.trade_markers = {'buy': [], 'sell': []}
        self.equity_curve = []
        self.position_curve = []
        self.dt_records = []

    def next(self):
        # 记录每根K线结束后的资金和仓位情况
        self.equity_curve.append(self.broker.getvalue())
        self.position_curve.append(self.position.size)
        self.dt_records.append(self.datas[0].datetime.datetime(0))
        
        if self.order:
            return  # 有挂单则不处理

        close = self.datas[0].close[0]
        ema_fast = self.ema_fast[0]
        ema_slow = self.ema_slow[0]

        # 信号过滤：仅在均线多头排列时做多，空头排列时做空
        if not self.position:
            if close > ema_slow and ema_fast > ema_slow:
                # 多头突破隧道，做多
                self.order = self.buy(size=self.p.size)
                self.buyprice = close
                self.log(f"买入信号: {close:.2f}")
            elif close < ema_fast and ema_fast < ema_slow:
                # 空头跌破隧道，做空
                self.order = self.sell(size=self.p.size)
                self.buyprice = close
                self.log(f"卖出信号: {close:.2f}")
        else:
            # 止损止盈逻辑
            if self.position.size > 0:
                # 多头持仓
                if close <= self.buyprice * (1 - self.p.stop_loss):
                    self.log(f"止损平多: {close:.2f}")
                    self.order = self.close()
                elif close >= self.buyprice * (1 + self.p.take_profit):
                    self.log(f"止盈平多: {close:.2f}")
                    self.order = self.close()
            elif self.position.size < 0:
                # 空头持仓
                if close >= self.buyprice * (1 + self.p.stop_loss):
                    self.log(f"止损平空: {close:.2f}")
                    self.order = self.close()
                elif close <= self.buyprice * (1 - self.p.take_profit):
                    self.log(f"止盈平空: {close:.2f}")
                    self.order = self.close()

    def log(self, txt, dt=None):
        dt = dt or self.datas[0].datetime.date(0)
        print(f'{dt.isoformat()}, {txt}')

    def notify_order(self, order):
        if order.status in [order.Completed]:
            dt = self.datas[0].datetime.datetime(0)
            if order.isbuy():
                self.log(f'买入成交: {order.executed.price:.2f}')
                self.trade_markers['buy'].append((dt, order.executed.price))
            elif order.issell():
                self.log(f'卖出成交: {order.executed.price:.2f}')
                self.trade_markers['sell'].append((dt, order.executed.price))
            self.buyprice = order.executed.price
            self.buycomm = order.executed.comm
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('订单取消/拒绝/保证金不足')
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f'交易结束, 毛利润: {trade.pnl:.2f}, 净利润: {trade.pnlcomm:.2f}')


def plot_results(df_plot, strat, initial_cash=1000000.0):
    """
    封装策略回测后的可视化绘图逻辑
    """
    # ---------------- 绘图设置 ----------------
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(14, 14), sharex=True, gridspec_kw={'height_ratios': [3, 1, 1, 1]})
    fig.canvas.manager.set_window_title('Backtest Results - Vegas Tunnel')
    
    x_dt = df_plot.index
    x_num = mdates.date2num(x_dt.to_pydatetime())
    
    # 1. 价格与 EMA
    ax1.plot(x_dt, df_plot['close'].values, '-', label='Close Price', color='#1f77b4', linewidth=1.2)
    ax1.plot(x_dt, df_plot['ema_fast'].values, '--', label='EMA(144)', color='#ff7f0e', linewidth=1.5)
    ax1.plot(x_dt, df_plot['ema_slow'].values, '--', label='EMA(169)', color='#2ca02c', linewidth=1.5)
    
    # 填充 Vegas 通道
    ax1.fill_between(x_dt, df_plot['ema_fast'].values, df_plot['ema_slow'].values, color='gray', alpha=0.2, label='Vegas Tunnel')
    
    # 绘制买卖点
    buys_dt = [m[0] for m in strat.trade_markers['buy']]
    buys_p = [m[1] for m in strat.trade_markers['buy']]
    sells_dt = [m[0] for m in strat.trade_markers['sell']]
    sells_p = [m[1] for m in strat.trade_markers['sell']]
    
    if buys_dt:
        ax1.scatter(buys_dt, buys_p, marker='^', color='red', s=120, label='Buy', zorder=5)
    if sells_dt:
        ax1.scatter(sells_dt, sells_p, marker='v', color='green', s=120, label='Sell', zorder=5)
        
    ax1.set_title('BTC-USD Trading Strategy: Vegas Tunnel', fontsize=14, fontweight='bold')
    ax1.set_ylabel('Price (USD)', fontsize=12)
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)
    
    # 2. 资金曲线 (Equity)
    ax2.plot(strat.dt_records, strat.equity_curve, '-', color='purple', linewidth=1.5, label='Portfolio Value')
    
    # 盈亏分色填充
    ax2.fill_between(strat.dt_records, strat.equity_curve, initial_cash, 
                     where=(np.array(strat.equity_curve) >= initial_cash), color='red', alpha=0.2, interpolate=True)
    ax2.fill_between(strat.dt_records, strat.equity_curve, initial_cash, 
                     where=(np.array(strat.equity_curve) < initial_cash), color='green', alpha=0.2, interpolate=True)
    
    ax2.set_ylabel('Equity', fontsize=12)
    ax2.legend(loc='upper left')
    ax2.grid(True, alpha=0.3)
    
    # 3. 仓位曲线 (Position)
    ax3.step(strat.dt_records, strat.position_curve, where='post', color='#17becf', linewidth=1.5, label='Position Size')
    ax3.fill_between(strat.dt_records, strat.position_curve, 0, step='post', alpha=0.3, color='#17becf')
    ax3.axhline(y=0, color='black', linewidth=1.0, alpha=0.5)
    ax3.set_ylabel('Position', fontsize=12)
    ax3.legend(loc='upper left')
    ax3.grid(True, alpha=0.3)
    
    # 4. 每日收益率柱状图 (Daily Returns)
    daily_returns = strat.analyzers.timereturn.get_analysis()
    dr_dates = [pd.to_datetime(d) for d in daily_returns.keys()]
    dr_values = [v * 100 for v in daily_returns.values()] # 转换为百分比
    
    colors = ['green' if val > 0 else 'red' for val in dr_values]
    # bar的宽度设为0.8（天）
    ax4.bar(dr_dates, dr_values, color=colors, width=0.8, alpha=0.7, label='Daily Return (%)')
    ax4.axhline(y=0, color='black', linewidth=1.0, alpha=0.5)
    
    ax4.set_ylabel('Daily Return (%)', fontsize=12)
    ax4.set_xlabel('Time', fontsize=12)
    ax4.legend(loc='upper left')
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
    
    for ann in [ann1, ann2, ann3, ann4]:
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
        y_ema_f = float(df_plot['ema_fast'].iat[idx])
        y_ema_s = float(df_plot['ema_slow'].iat[idx])
        
        # 匹配对应时间点的资金和仓位 (如果有)
        eq_idx = min(idx, len(strat.equity_curve) - 1)
        y_eq = float(strat.equity_curve[eq_idx]) if eq_idx >= 0 else initial_cash
        y_pos = float(strat.position_curve[eq_idx]) if eq_idx >= 0 else 0.0
        
        # 匹配对应时间点的日收益率
        dr_dates_num = mdates.date2num(dr_dates) if dr_dates else []
        if len(dr_dates_num) > 0:
            dr_idx = int(np.searchsorted(dr_dates_num, mx))
            dr_idx = max(0, min(dr_idx, len(dr_dates_num) - 1))
            y_dr = dr_values[dr_idx]
        else:
            y_dr = 0.0
            
        # 隐藏所有横线和注释
        for h in hlines: h.set_visible(False)
        for a in [ann1, ann2, ann3, ann4]: a.set_visible(False)
            
        if event.inaxes == ax1:
            ann1.xy = (xi, y_close)
            ann1.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nClose: {y_close:.2f}\nEMA144: {y_ema_f:.2f}\nEMA169: {y_ema_s:.2f}")
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
            ann3.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nPosition: {y_pos}")
            ann3.set_visible(True)
            hline3.set_ydata([y_pos, y_pos])
            hline3.set_visible(True)
        elif event.inaxes == ax4:
            ann4.xy = (xi, y_dr)
            ann4.set_text(f"{x_dt[idx].strftime('%Y-%m-%d')}\nReturn: {y_dr:.2f}%")
            ann4.set_visible(True)
            hline4.set_ydata([y_dr, y_dr])
            hline4.set_visible(True)
            
        for v in vlines:
            v.set_xdata([xi, xi])
            v.set_visible(True)
            
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect('motion_notify_event', on_move)
    plt.tight_layout()
    plt.show()

def my_strage():
    print("开始回测...")
    # 创建Cerebro引擎  
    cerebro = bt.Cerebro() 

    cerebro.addstrategy(VegasTunnelStrategy)
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
    cerebro.broker.setcommission(commission=0.001)
    
    # 新增：添加收益率分析器
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='timereturn')
    
    # 引擎运行前打印期出资金  
    print('组合期初资金: %.2f' % cerebro.broker.getvalue()) 
    results = cerebro.run() 
    strat = results[0]
    # 引擎运行后打期末资金  
    print('组合期末资金: %.2f' % cerebro.broker.getvalue())
    
    # 调用绘图函数
    plot_results(df_plot, strat, initial_cash=1000000.0)

if __name__ == "__main__":
    my_strage()
