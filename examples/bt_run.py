import backtrader as bt
import yfinance as yf
import pandas as pd
import os
import datetime
import sys # 获取当前运行脚本的路径 (in argv[0]) 
import matplotlib

class cBackTest(bt.Strategy):
    def log(self,txt,dt = None):
        dt = dt or self.datas[0].datetime.date(0) 
        print('%s, %s' % (dt.isoformat(), txt)) 

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None
        self.buyprice = None
        self.buycomm = None
        self.sellprice = None
        self.sellcomm = None
        self.ema12 = bt.indicators.ExponentialMovingAverage(self.datas[0], period=12)
        self.ema144 = bt.indicators.ExponentialMovingAverage(self.datas[0], period=144)
        self.ema169 = bt.indicators.ExponentialMovingAverage(self.datas[0], period=169)

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
                # broker 提交/接受了，买/卖订单则什么都不做  
                return

        # 检查一个订单是否完成  
        # 注意: 当资金不足时，broker会拒绝订单  
        if order.status in [order.Completed]:
            if order.isbuy():
                self.log(f"已买入, 价格: {order.executed.price:.2f}, 成本: {order.executed.value:.2f}, 手续费: {order.executed.comm:.2f}")
                self.buyprice = order.executed.price
                self.buycomm = order.executed.comm
            elif order.issell():
                self.log(f"已卖出, 价格: {order.executed.price:.2f}, 成本: {order.executed.value:.2f}, 手续费: {order.executed.comm:.2f}")
                self.sellprice = order.executed.price
                self.sellcomm = order.executed.comm
            #记录交易数量
            self.bar_executed = len(self)
        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            self.log('订单取消/保证金不足/拒绝')
        # 其他状态记录为：无挂起订单 
        self.order = None

    def notify_trade(self, trade):
        if not trade.isclosed:
            return
        self.log(f"已平仓, 交易利润, 毛利润 {trade.pnl:.2f}, 净利润 {trade.pnlcomm:.2f}")

    def next(self):
        self.log(f"Close: {self.dataclose[0]}")
       
        if not self.position:
             # 今天的收盘价 < 昨天收盘价
                if self.dataclose[0] >= self.ema169[0] and self.dataclose[0] <= self.ema144[0]:
                    self.log('买入, %.2f' % self.dataclose[0])
                    self.order = self.buy()
        else:
            if self.dataclose[0] < self.ema169[0]:
                self.log('卖出, %.2f' % self.dataclose[0])
                self.order = self.sell()

def my_strage():
    print("开始回测...")
    # 创建Cerebro引擎  
    cerebro = bt.Cerebro() 

    cerebro.addstrategy(cBackTest)
    # 获取当前运行脚本所在目录  
    modpath = os.path.dirname(os.path.abspath(sys.argv[0]))

    # 用pandas读取CSV数据
    df = pd.read_csv(os.path.join(modpath, '../BTC-USD_1H_20251111_222737.csv'), parse_dates=['datetime'])
    print("数据长度：", len(df))  # df 是你的 DataFrame
    df.set_index('datetime', inplace=True)
    data = bt.feeds.PandasData(
        dataname=df,
        fromdate=datetime.datetime(2025, 11,6, 14, 30, 0),
        todate=datetime.datetime(2025, 11, 9, 23,55, 0)
    )
    cerebro.adddata(data)
    # 设置投资金额100000.0 
    cerebro.broker.setcash(100000.0) 
    cerebro.broker.setcommission(commission=0.001)
    # 引擎运行前打印期出资金  
    print('组合期初资金: %.2f' % cerebro.broker.getvalue()) 
    cerebro.run() 
    # 引擎运行后打期末资金  
    print('组合期末资金: %.2f' % cerebro.broker.getvalue())
    cerebro.plot()

if __name__ == "__main__":
    my_strage()



