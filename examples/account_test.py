# 说明：
# 本文件演示使用 ccxt 操作 OKX 合约账户，包括：
# 1) 设置杠杆、查询余额与保证金判定
# 2) 根据最近成交记录，查询对应交易对的持仓并进行平仓（reduceOnly 市价）
#
# 注意：
# - apiKey/secret/password 建议使用环境变量或配置文件注入，避免明文保存在代码里
# - 对于合约，OKX 的 symbol 形如 'BTC/USDT:USDT'，并需要在 params 中指明类型 {'type': 'swap'}
# - 下单参数 tdMode='cross' 表示全仓，reduceOnly=True 表示仅平仓不会开新仓
#
import unittest
import ccxt
import pandas as pd
import time

okx = ccxt.okx({
    'apiKey': 'yours apiKey',
    'secret': 'yours secret',
    'password': 'yours password',
    'options': {
        'defaultType': 'option',
    },
    'enableRateLimit':True,
    # 'proxies': {
    #     'http': 'http://127.0.0.1:7890',
    #     'https': 'http://127.0.0.1:7890',
    # }
    })
def test_account_bills():
    print('账户账单信息:', okx.fetch_balance())
 
def swap_long():
    # 目标交易对与下单方向
    symbol = 'BTC/USDT:USDT'
    type = 'limit'
    side = 'buy'
    btc_amount = 10  # 我希望交易的 BTC 数量（按币数）
    contract_size = 0.01  # 面值（通常从 market['contractSize'] 获取）
    amount = btc_amount * contract_size  # 需要的合约数量，例如 10 * 0.01 = 0.1

    price  = 70724  # 当前价格（示例）

    params = {
        'positionSide':'long',  # 多头
        'leverage':5,           # 杠杆
    }
    # 设置杠杆（全仓）
    leverage_result = okx.set_leverage(leverage= 5,symbol = symbol, params={'marginMode':'cross'})
    print('设置杠杆结果:',leverage_result)

    # 查询余额（合约账户）
    balance_result = okx.fetch_balance({'type':'swap'})
    usdt_balance = float(balance_result['free'].get('USDT',0))
   
    # 计算保证金需求：
    # 合约总价值 = 合约数 * 价格 * 面值；所需保证金 = 合约总价值 / 杠杆
    contract_value = amount * price * contract_size  # 例如 0.1 * 70000 * 0.01 = 70
    required_margin = contract_value / params['leverage']  # 例如 70 / 5 = 14
    print('计划交易btc数量:',btc_amount)
    print('对应合约数量:',amount)
    print(' USDT余额:',usdt_balance,' 所需保证金:',required_margin)

    # 判断是否有足够的保证金
    if usdt_balance < required_margin:
        print('USDT余额不足,无法交易')
    else:
        print('USDT余额充足,可以交易')
    
    # 示例：实际下单（当前注释掉，避免真实交易）
    # order = okx.create_order(symbol, type, side, amount, price, {'tdMode': 'cross', 'posSide': 'long'})
    # print('下单结果:', order)
    # try:
    #     open_orders = okx.fetch_open_orders('BTC/USDT:USDT')
    #     for o in open_orders:
    #         okx.cancel_order(o['id'], 'BTC/USDT:USDT')
    #         print('取消订单结果:', o['id'])
    # except Exception as e:
    #     print('下单失败:',e)
    # trades = okx.fetchMyTrades('BTC/USDT:USDT', limit=50, params={'type': 'swap'})
    # for t in trades:
    # res = okx.cancel_order('2421160644', 'BTC/USDT:USDT')
    # print('取消订单结果:',res)
def close_positions_from_recent_trades():
    # 根据最近成交记录推断涉及到的交易对，然后查询持仓并进行平仓（reduceOnly）
    try:
        markets = okx.load_markets()  # 预加载市场信息（便于后续解析）
        trades = okx.fetchMyTrades('BTC/USDT:USDT', limit=50, params={'type': 'swap'})  # 最近成交
        symbols = set([t.get('symbol') for t in trades if t.get('symbol')])  # 提取涉及到的 symbol
        if not symbols:
            symbols = {'BTC/USDT:USDT'}  # 若最近无成交，默认尝试 BTC 合约
        for sym in symbols:
            try:
                # 查询该交易对的持仓；返回列表，包含多空与数量信息
                positions = okx.fetch_positions([sym], params={'type': 'swap'})
            except Exception:
                positions = []
            for p in positions:
                # 兼容不同字段：side/posSide 表示方向；contracts/amount/pos 表示数量
                side = p.get('side') or (p.get('info', {}).get('posSide') or '')
                amount = p.get('contracts') or p.get('amount') or float(p.get('info', {}).get('pos', 0) or 0)
                if amount and amount > 0:
                    if side.lower() == 'long':
                        # 平多：市价卖出，reduceOnly 确保不加仓
                        order = okx.create_order(sym, 'market', 'sell', amount, None, {'reduceOnly': True, 'tdMode': 'cross', 'posSide': 'long'})
                        print('平多结果:', order.get('id'))
                    elif side.lower() == 'short':
                        # 平空：市价买入，reduceOnly 确保不加仓
                        order = okx.create_order(sym, 'market', 'buy', amount, None, {'reduceOnly': True, 'tdMode': 'cross', 'posSide': 'short'})
                        print('平空结果:', order.get('id'))
    except Exception as e:
        print('平仓失败:', e)

if __name__ == '__main__':
    # 直接运行平仓操作；如需演示下单与保证金判定，可调用 swap_long()
    close_positions_from_recent_trades()
