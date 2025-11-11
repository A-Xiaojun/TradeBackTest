import pandas as pd
import requests
import time
import json
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

class OKXDataFetcher:
    def __init__(self, demo=True, proxies=None):
        """
        初始化OKX数据获取器
        demo: True使用模拟交易环境，False使用实盘环境
        proxies: 代理设置，字典类型
        """
        self.base_url = "https://www.okx.com"
        self.proxies = proxies
        if demo:
            self.base_url = "https://www.okx.com"  # 公开API不需要demo环境

    def get_klines(self, instId="BTC-USDT", bar="1H", limit=100, start_time=None, end_time=None):
        """
        获取K线数据
        参数:
        instId: 交易对，如 BTC-USDT, ETH-USDT
        bar: K线周期 - 1m, 5m, 15m, 1H, 4H, 1D, 1W
        limit: 获取数量，最大100
        start_time: 开始时间 (ISO格式或时间戳)
        end_time: 结束时间 (ISO格式或时间戳)
        """
        url = f"{self.base_url}/api/v5/market/candles"
        params = {
            'instId': instId,
            'bar': bar,
            'limit': limit
        }
        # 添加时间参数
        if start_time:
            if isinstance(start_time, str):
                start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                params['before'] = str(int(start_dt.timestamp() * 1000))
            else:
                params['before'] = str(start_time)
        if end_time:
            if isinstance(end_time, str):
                end_dt = datetime.fromisoformat(end_time.replace('Z', '+00:00'))
                params['after'] = str(int(end_dt.timestamp() * 1000))
            else:
                params['after'] = str(end_time)
        try:
            response = requests.get(url, params=params, timeout=10, proxies=self.proxies)
            data = response.json()
            if data['code'] == '0':
                kline_data = data['data']
                df = self._parse_klines(kline_data)
                return df
            else:
                print(f"获取数据失败: {data['msg']}")
                return None
        except Exception as e:
            print(f"请求出错: {e}")
            return None

    def get_ticker(self, instId="BTC-USDT"):
        """获取单个币种的ticker信息"""
        url = f"{self.base_url}/api/v5/market/ticker"
        params = {'instId': instId}
        try:
            response = requests.get(url, params=params, timeout=10, proxies=self.proxies)
            data = response.json()
            if data['code'] == '0':
                return data['data'][0]
            else:
                print(f"获取ticker失败: {data['msg']}")
                return None
        except Exception as e:
            print(f"请求出错: {e}")
            return None

    def get_instruments(self, instType="SPOT"):
        """获取可交易产品列表"""
        url = f"{self.base_url}/api/v5/public/instruments"
        params = {'instType': instType}
        try:
            response = requests.get(url, params=params, timeout=10, proxies=self.proxies)
            data = response.json()
            if data['code'] == '0':
                return pd.DataFrame(data['data'])
            else:
                print(f"获取产品列表失败: {data['msg']}")
                return None
        except Exception as e:
            print(f"请求出错: {e}")
            return None
    
    def _parse_klines(self, kline_data):
        """解析K线数据"""
        columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'volume_currency', 'buy_sell', 'confirm']
        df = pd.DataFrame(kline_data, columns=columns)
        # 转换数据类型
        df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
        for col in ['open', 'high', 'low', 'close', 'volume', 'volume_currency']:
            df[col] = df[col].astype(float)
        df.set_index('timestamp', inplace=True)
        df.sort_index(inplace=True)
        return df
    
    def get_historical_klines(self, instId="BTC-USDT", bar="1H", days=30):
        """
        获取多天的历史K线数据
        通过分页请求获取大量数据
        """
        all_data = []
        end_time = None
        
        print(f"开始获取 {instId} 的 {days} 天 {bar} K线数据...")
        
        for i in range(100):  # 最多100次请求，防止无限循环
            df = self.get_klines(
                instId=instId, 
                bar=bar, 
                limit=100,
                end_time=end_time
            )
            
            if df is None or len(df) == 0:
                break
                
            all_data.append(df)
            
            # 更新结束时间为最早的时间戳
            earliest_time = df.index.min()
            end_time = int(earliest_time.timestamp() * 1000)
            
            print(f"已获取 {len(all_data) * 100} 条K线...")
            
            # 检查是否达到所需天数
            if len(all_data) > 0:
                total_days = (all_data[0].index.max() - earliest_time).days
                if total_days >= days:
                    break
            
            time.sleep(0.1)  # 避免请求过于频繁
        
        if all_data:
            result_df = pd.concat(all_data)
            result_df = result_df[~result_df.index.duplicated(keep='first')]
            result_df.sort_index(inplace=True)
            print(f"数据获取完成！总共获取 {len(result_df)} 条K线数据")
            return result_df
        else:
            print("未能获取到数据")
            return None

# 使用示例
if __name__ == "__main__":
    # 创建数据获取器实例
    proxies = {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897"
    }
    fetcher = OKXDataFetcher(proxies=proxies)
    print("=" * 50)
    print("OKX 数据获取示例")
    print("=" * 50)
    # 获取BTC-USD 30天1小时K线数据
    btc_30d = fetcher.get_historical_klines(instId="BTC-USD", bar="5m", days=10)
    if btc_30d is not None:
        print(f"获取到 {len(btc_30d)} 条K线")
        print(btc_30d.head())
        # 保存到CSV文件
        filename = f"BTC-USD_1H_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        btc_30d.to_csv(filename)
        print(f"数据已保存到: {filename}")
    print("\n" + "=" * 50)
    print("数据获取完成！")
    print("=" * 50)