import backtrader as bt
import yfinance as yf
import pandas as pd
import os
import datetime
import sys # 获取当前运行脚本的路径 (in argv[0]) 
import matplotlib

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

    def next(self):
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
            if order.isbuy():
                self.log(f'买入成交: {order.executed.price:.2f}')
            elif order.issell():
                self.log(f'卖出成交: {order.executed.price:.2f}')
            self.buyprice = order.executed.price
            self.buycomm = order.executed.comm
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('订单取消/拒绝/保证金不足')
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f'交易结束, 毛利润: {trade.pnl:.2f}, 净利润: {trade.pnlcomm:.2f}')


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
    data = bt.feeds.PandasData(
        dataname=df,
        timeframe=bt.TimeFrame.Minutes,  # 明确指定为分钟级别
        compression=60,                  # 1小时K线
        fromdate=datetime.datetime(2025, 9, 18, 11, 0, 0),
        todate=datetime.datetime(2025, 11, 11, 14,0, 0)
    )
    cerebro.adddata(data)
    # 设置投资金额100000.0 
    cerebro.broker.setcash(1000000.0) 
    cerebro.broker.setcommission(commission=0.001)
    # 引擎运行前打印期出资金  
    print('组合期初资金: %.2f' % cerebro.broker.getvalue()) 
    cerebro.run() 
    # 引擎运行后打期末资金  
    print('组合期末资金: %.2f' % cerebro.broker.getvalue())
    cerebro.plot()

if __name__ == "__main__":
    my_strage()



