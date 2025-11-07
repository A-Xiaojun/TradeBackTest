import backtrader as bt
import yfinance as yf
import pandas as pd
import os
import datetime
import sys # 获取当前运行脚本的路径 (in argv[0]) 
# import matplotlib

class cBackTest(bt.Strategy):
    def log(self,txt,dt = None):
        dt = dt or self.datas[0].datetime.date(0) 
        print('%s, %s' % (dt.isoformat(), txt)) 

    def __init__(self):
        self.dataclose = self.datas[0].close

    def next(self):
        self.log(f"Close: {self.dataclose[0]}")
        if(self.dataclose[0] < self.dataclose[-1]):
            if(self.dataclose[0] < self.dataclose[-2]):
                self.log('买入, %.2f' % self.dataclose[0])
                self.buy()

def my_strage():
    print("开始回测...")
    # 创建Cerebro引擎  
    cerebro = bt.Cerebro() 

    cerebro.addstrategy(cBackTest)
    # 获取当前运行脚本所在目录  
    modpath = os.path.dirname(os.path.abspath(sys.argv[0]))

    # 用pandas读取CSV数据
    df = pd.read_csv(os.path.join(modpath, './TSLA_data.csv'), parse_dates=['datetime'])
    df.set_index('datetime', inplace=True)
    data = bt.feeds.PandasData(
        dataname=df,
        fromdate=datetime.datetime(2023, 1, 3),
        todate=datetime.datetime(2023, 12, 29)
    )
    cerebro.adddata(data)

    # 设置投资金额100000.0 
    cerebro.broker.setcash(100000.0) 

    # 引擎运行前打印期出资金  
    print('组合期初资金: %.2f' % cerebro.broker.getvalue()) 
    cerebro.run() 
    # 引擎运行后打期末资金  
    print('组合期末资金: %.2f' % cerebro.broker.getvalue())
    

if __name__ == "__main__":
    my_strage()



