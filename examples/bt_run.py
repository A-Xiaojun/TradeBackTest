import backtrader as bt
import pandas as pd
import os
import datetime
import sys # 获取当前运行脚本的路径 (in argv[0]) 
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

# ---------------- 数据源配置（只需改这里） ----------------
DATA_SOURCES = {
    "eth_1h": "ETHUSD_1H_20260413_104040.csv",
    "btc_1h_new": "BTCUSD_1H_20260412_062757.csv",
    "btc_1h_old": "BTC-USD_1H_20251111_221506.csv",
    "btc_15m": "BTCUSD_15M_20260328_131652.csv",
    "eth_15m_105553": "ETHUSD_15M_20260413_105553.csv",
    "eth_15m_new": "ETHUSD_15M_20260413_115652.csv",
    "eth_15m_2022":"ETHUSD_15M_20260502_144301.csv"
}
ACTIVE_DATA_SOURCE = "eth_15m_2022"  # 在这里切换数据源键
DATA_SOURCE_SUBDIR = os.path.join("output", "kline")
BACKTEST_INITIAL_CASH = 750.0  # 初始资金（USDT）

class RightSidePivotStrategy(bt.Strategy):
    """
    右侧交易策略：
    - 做多：寻找越来越高的低点 (Higher Lows)。当一轮小回调结束，价格重新向上拐头突破时入场。
    - 做空：寻找越来越低的高点 (Lower Highs)。当反弹结束，价格重新向下拐头跌破时入场。
    - 仓位：每次信号只使用当前总资金的 10% 进行开仓。
    - 止损：做多的止损设在最近一个波谷（低点），做空的止损设在最近一个波峰（高点）。
    """
    params = (
        ('pivot_period', 8),  # 寻找局部高低点的窗口期（左右各看几根K线）
        ('risk_percent', 1.00), # 每次开仓使用资金比例 (100%，即全仓)
        ('leverage', 10.0),   # 新增：杠杆倍数，默认 10x
        ('order_utilization', 1.00),  # 可用保证金利用率（全仓）
        ('auto_topup_to_initial', True),  # 当前资金低于期初时，自动补到期初
        ('auto_withdraw_profit', True),   # 当前资金高于期初时，自动取出利润
        ('max_external_topup', 2600.0),   # 后备资金上限（总额，USDT）
        ('profit_pool_floor', 50.0),      # 盈利池保底余额（尽量不补到0）
        ('atr_period', 14),
        ('adx_period', 14),
        ('adx_threshold', 18.0),
        ('bb_period', 20),
        ('bb_dev', 2.0),
        ('bb_bandwidth_threshold', 0.010),
        ('min_swing_atr_mult', 0.6),
        ('breakout_buffer_atr_mult', 0.10),
        ('min_ema_spread_pct', 0.003),  # EMA发散阈值：过小视为震荡
        ('cooldown_bars', 2),
        ('debug_decision', True),  # 是否记录逐根K线决策日志
    )

    def __init__(self):
        self.order = None
        self.buyprice = None
        self.buycomm = None
        self.stop_price = None
        self.initial_equity = None  # 记录期初资金，用于固定仓位
        # 资金流记账（用于计算真实盈亏）
        self.cum_withdraw = 0.0          # 累计取出
        self.cum_topup_external = 0.0    # 累计外部补资
        self.profit_pool = 0.0           # 盈利池（从账户取出的利润，可回补）
        self.funding_exhausted = False   # 后备资金耗尽后，不再新开仓
        self.funding_exhausted_notified = False

        # 记录资金曲线、买卖点和仓位以便绘图
        self.trade_markers = {'buy': [], 'sell': []}
        self.equity_curve = []
        self.position_curve = []
        self.position_value_curve = [] # 新增：记录仓位的名义USDT价值
        self.realized_pnl = 0.0         # 累计已平仓净盈亏（仅按平仓trade.pnlcomm累加）
        self.real_pnl_curve = []        # 累计已平仓净盈亏曲线（用于展示/统计）
        self.withdraw_curve = []        # 累计取出曲线
        self.external_topup_curve = []  # 累计外部补资曲线
        self.profit_pool_curve = []     # 盈利池余额曲线
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
        self.decision_log_path = None
        if self.p.debug_decision:
            out_dir = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), 'output')
            os.makedirs(out_dir, exist_ok=True)
            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            self.decision_log_path = os.path.join(out_dir, f"decision_log_{ACTIVE_DATA_SOURCE}_{ts}.log")
            with open(self.decision_log_path, "a", encoding="utf-8") as fh:
                fh.write("datetime,stage,reason,close,extras\n")
            self.log(f"决策日志已写入: {self.decision_log_path}")
        
        # 记录给绘图用（占位，保持兼容原有绘图代码逻辑）
        # 如果需要可以在图中绘制，这里复用 ema_fast / ema_slow 的变量名来占位以防报错
        self.ema_fast = bt.indicators.SMA(self.datas[0], period=10) 
        self.ema_slow = bt.indicators.SMA(self.datas[0], period=20)
        # 震荡/波动过滤指标（不用于趋势方向，只用于去噪）
        self.atr = bt.indicators.ATR(self.datas[0], period=self.p.atr_period)
        self.adx = bt.indicators.AverageDirectionalMovementIndex(self.datas[0], period=self.p.adx_period)
        self.bb = bt.indicators.BollingerBands(self.datas[0], period=self.p.bb_period, devfactor=self.p.bb_dev)

    def get_real_monthly_returns(self):
        """按已平仓累计盈亏口径统计月度收益率。"""
        if not self.dt_records or not self.real_pnl_curve:
            return {}
        real_equity = np.asarray(self.real_pnl_curve, dtype=float) + float(self.initial_equity)
        idx = pd.DatetimeIndex(self.dt_records)
        s = pd.Series(real_equity, index=idx).sort_index()
        month_end_equity = s.groupby(s.index.to_period('M')).last()

        out = {}
        prev = float(self.initial_equity)
        for p, v in month_end_equity.items():
            cur = float(v)
            ret = (cur / prev - 1.0) if prev > 0 else np.nan
            out[str(p)] = ret
            prev = cur
        return out

    def get_profit_loss_stats(self):
        """统计盈利/亏损汇总，并按总盈利/总亏损计算盈亏比。"""
        if not self.trade_pnls:
            return None

        pnls = np.asarray([pnl for _, pnl in self.trade_pnls], dtype=float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        total_win = float(wins.sum()) if wins.size else 0.0
        total_loss = float(np.abs(losses.sum())) if losses.size else 0.0
        avg_win = float(wins.mean()) if wins.size else 0.0
        avg_loss = float(np.abs(losses.mean())) if losses.size else 0.0
        profit_loss_ratio = (total_win / total_loss) if total_loss > 0 else np.inf

        return {
            'win_count': int(wins.size),
            'loss_count': int(losses.size),
            'total_win': total_win,
            'total_loss': total_loss,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_loss_ratio': profit_loss_ratio,
        }

    def _apply_cashflow_policy(self):
        """空仓时执行资金流策略：盈利取出、亏损回补（优先盈利池）。"""
        if self.initial_equity is None:
            return
        current_value = float(self.broker.getvalue())
        eps = 1e-9
        if self.p.auto_withdraw_profit and current_value > self.initial_equity + eps:
            amount = current_value - self.initial_equity
            if hasattr(self.broker, "add_cash"):
                self.broker.add_cash(-amount)
            else:
                self.broker.setcash(float(self.broker.getcash()) - amount)
            self.cum_withdraw += amount
            self.profit_pool += amount
            self.log(f"盈利取出: {amount:.2f}, 盈利池={self.profit_pool:.2f}")
            return

        if self.p.auto_topup_to_initial and current_value < self.initial_equity - eps:
            deficit = self.initial_equity - current_value
            pool_usable = max(self.profit_pool - self.p.profit_pool_floor, 0.0)
            from_pool = min(deficit, pool_usable)
            remain = deficit - from_pool
            external_left = max(self.p.max_external_topup - self.cum_topup_external, 0.0)
            external = min(remain, external_left) if remain > eps else 0.0
            topup = from_pool + external
            if topup <= eps:
                self.funding_exhausted = True
                if not self.funding_exhausted_notified:
                    self.log(f"补资失败: 盈利池与后备资金均耗尽(后备上限={self.p.max_external_topup:.2f})，停止新开仓")
                    self.funding_exhausted_notified = True
                return
            if hasattr(self.broker, "add_cash"):
                self.broker.add_cash(topup)
            else:
                self.broker.setcash(float(self.broker.getcash()) + topup)
            self.profit_pool -= from_pool
            self.cum_topup_external += external
            self.log(f"亏损补资: {topup:.2f} (盈利池={from_pool:.2f}, 外部={external:.2f}), 盈利池={self.profit_pool:.2f}, 外部累计={self.cum_topup_external:.2f}")
            if topup + eps < deficit:
                self.funding_exhausted = True
                if not self.funding_exhausted_notified:
                    self.log(f"后备资金已达上限 {self.p.max_external_topup:.2f}，仍缺口={deficit-topup:.2f}，停止新开仓")
                    self.funding_exhausted_notified = True

    def _can_continue_trading_when_funding_exhausted(self) -> bool:
        """资金耗尽后，若真实累计盈亏为正或后备资金仍有余额，则允许继续交易。"""
        external_left = self.p.max_external_topup - self.cum_topup_external
        current_real_pnl = float(self.broker.getvalue()) + self.cum_withdraw - self.cum_topup_external - float(self.initial_equity or 0.0)
        return (external_left > 1e-9) or (current_real_pnl > 0.0)

    def _calc_order_size(self, close: float) -> float:
        """按期初资金目标下单，并受当前可用保证金约束，尽量避免保证金拒单。"""
        if close <= 0:
            return 0.0
        target_value = self.initial_equity * self.p.risk_percent * self.p.leverage
        available_cash = max(float(self.broker.getcash()), 0.0)
        max_notional_now = available_cash * self.p.leverage * self.p.order_utilization
        notional = min(target_value, max_notional_now)
        size = notional / close
        return max(size, 0.0)

    def _log_decision(self, stage: str, reason: str, close: float, **kwargs):
        """将逐根K线决策写入单独日志文件，便于排查为何下单/未下单。"""
        if not self.p.debug_decision or not self.decision_log_path:
            return
        dt = self.datas[0].datetime.datetime(0).isoformat()
        extras = ";".join([f"{k}={v}" for k, v in kwargs.items()]) if kwargs else ""
        with open(self.decision_log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{dt},{stage},{reason},{close:.8f},{extras}\n")

    def next(self):
        if self.initial_equity is None:
            self.initial_equity = float(self.broker.getvalue())
        # 仅在空仓且无挂单时补资，避免干扰在途订单与持仓估值
        if not self.position and not self.order:
            self._apply_cashflow_policy()

        # 记录每根K线结束后的资金和仓位情况
        self.equity_curve.append(self.broker.getvalue())
        self.position_curve.append(self.position.size)
        # “真实累计盈亏”统一为仅按平仓净利润累加，不受浮动盈亏与资金流影响
        self.real_pnl_curve.append(float(self.realized_pnl))
        self.withdraw_curve.append(self.cum_withdraw)
        self.external_topup_curve.append(self.cum_topup_external)
        self.profit_pool_curve.append(self.profit_pool)
        
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
            self._log_decision("skip", "pending_order", current_close)
            return  # 有挂单则不处理
        if (not self.position) and self.funding_exhausted:
            if not self._can_continue_trading_when_funding_exhausted():
                self._log_decision("skip", "funding_exhausted", current_close)
                return  # 资金补充能力已耗尽，且无继续交易条件

        # 寻找分形高低点 (Fractal Pivots)
        # 判断当前K线往前推 pivot_period 根K线，是否是局部最高或最低
        p = self.p.pivot_period
        
        # 确保有足够的数据历史
        if len(self.datas[0]) < p * 2 + 1:
            self._log_decision("skip", "insufficient_history", current_close, need=p * 2 + 1, have=len(self.datas[0]))
            return

        # 获取前后 p 根 K 线的最高价和最低价数组
        high_slice = self.datas[0].high.get(ago=-p, size=p*2+1)
        low_slice = self.datas[0].low.get(ago=-p, size=p*2+1)
        
        if len(high_slice) != p*2+1:
            self._log_decision("skip", "invalid_pivot_window", current_close, expected=p * 2 + 1, actual=len(high_slice))
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
            self._log_decision(
                "skip",
                "cooldown",
                close,
                bars_since_exit=(self.bar_count - self.last_exit_bar),
                cooldown_bars=self.p.cooldown_bars,
            )
            return
        ema_spread = 0.0
        if close != 0:
            ema_spread = abs(self.ema_fast[0] - self.ema_slow[0]) / abs(close)

        # ---------------- 交易逻辑 ----------------
        if not self.position:
            # 震荡过滤仅用于“开仓判断”，不阻断持仓后的止损/离场
            if (self.adx[0] < self.p.adx_threshold) or (bb_width < self.p.bb_bandwidth_threshold) or (ema_spread < self.p.min_ema_spread_pct):
                self._log_decision(
                    "skip",
                    "chop_filter",
                    close,
                    adx=f"{self.adx[0]:.2f}",
                    adx_threshold=f"{self.p.adx_threshold:.2f}",
                    bb_width=f"{bb_width:.4f}",
                    bb_threshold=f"{self.p.bb_bandwidth_threshold:.4f}",
                    ema_spread=f"{ema_spread:.4f}",
                    ema_threshold=f"{self.p.min_ema_spread_pct:.4f}",
                )
                return
            # 1. 寻找做多机会：形成越来越高的低点 (Higher Lows) 即入场
            if len(self.lows) >= 2:
                # 判断最近两个低点是否抬高
                swing_ok = (self.lows[-1] - self.lows[-2]) >= self.p.min_swing_atr_mult * max(self.atr[0], 1e-9)
                if self.lows[-1] > self.lows[-2] and swing_ok:
                    if self.lows[-1] >= close:
                        self._log_decision(
                            "skip",
                            "invalid_long_stop_relation",
                            close,
                            stop=f"{self.lows[-1]:.2f}",
                        )
                        return
                    size = self._calc_order_size(close)
                    if size <= 0:
                        self._log_decision("skip", "size_zero_long", close)
                        return
                    self._log_decision(
                        "entry",
                        "higher_low",
                        close,
                        stop=f"{self.lows[-1]:.2f}",
                        size=f"{size:.6f}",
                        low_prev=f"{self.lows[-2]:.2f}",
                        low_last=f"{self.lows[-1]:.2f}",
                    )
                    self.order = self.buy(size=size)
                    self.stop_price = self.lows[-1] # 止损设在最近的低点拐点
                    self.log(f"做多信号(Higher Low): 价格={close:.2f}, 止损={self.stop_price:.2f}, 杠杆={self.p.leverage}x")

            # 2. 寻找做空机会：形成越来越低的高点 (Lower Highs) 即入场
            if len(self.highs) >= 2 and not self.order:
                # 判断最近两个高点是否降低
                swing_ok = (self.highs[-2] - self.highs[-1]) >= self.p.min_swing_atr_mult * max(self.atr[0], 1e-9)
                if self.highs[-1] < self.highs[-2] and swing_ok:
                    if self.highs[-1] <= close:
                        self._log_decision(
                            "skip",
                            "invalid_short_stop_relation",
                            close,
                            stop=f"{self.highs[-1]:.2f}",
                        )
                        return
                    size = self._calc_order_size(close)
                    if size <= 0:
                        self._log_decision("skip", "size_zero_short", close)
                        return
                    self._log_decision(
                        "entry",
                        "lower_high",
                        close,
                        stop=f"{self.highs[-1]:.2f}",
                        size=f"{size:.6f}",
                        high_prev=f"{self.highs[-2]:.2f}",
                        high_last=f"{self.highs[-1]:.2f}",
                    )
                    self.order = self.sell(size=size)
                    self.stop_price = self.highs[-1] # 止损设在最近的高点拐点
                    self.log(f"做空信号(Lower High): 价格={close:.2f}, 止损={self.stop_price:.2f}, 杠杆={self.p.leverage}x")

        else:
            # ---------------- 止损/平仓逻辑 ----------------
            if self.position.size > 0:
                # 多头止损
                if close <= self.stop_price:
                    self._log_decision("exit", "long_stop", close, stop=f"{self.stop_price:.2f}")
                    self.log(f"多头触及拐点止损平仓: {close:.2f} (止损价: {self.stop_price:.2f})")
                    self.order = self.close()
                # 简单止盈：如果出现了降低的高点，说明上涨动能衰竭，平多
                elif len(self.highs) >= 2 and self.highs[-1] < self.highs[-2]:
                    self._log_decision("exit", "long_momentum_exhausted", close)
                    self.log(f"多头动能衰竭(出现Lower High)平仓: {close:.2f}")
                    self.order = self.close()
                    
            elif self.position.size < 0:
                # 空头止损
                if close >= self.stop_price:
                    self._log_decision("exit", "short_stop", close, stop=f"{self.stop_price:.2f}")
                    self.log(f"空头触及拐点止损平仓: {close:.2f} (止损价: {self.stop_price:.2f})")
                    self.order = self.close()
                # 简单止盈：如果出现了抬高的低点，说明下跌动能衰竭，平空
                elif len(self.lows) >= 2 and self.lows[-1] > self.lows[-2]:
                    self._log_decision("exit", "short_momentum_exhausted", close)
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
            self.log(f'订单取消/拒绝/保证金不足: cash={self.broker.getcash():.2f}, value={self.broker.getvalue():.2f}')
        self.order = None

    def notify_trade(self, trade):
        if trade.isclosed:
            self.log(f'交易结束, 毛利润: {trade.pnl:.2f}, 净利润: {trade.pnlcomm:.2f}')
            self.stop_price = None
            dt = self.datas[0].datetime.datetime(0)
            self.trade_pnls.append((dt, float(trade.pnlcomm)))
            self.realized_pnl += float(trade.pnlcomm)
            # 记录平仓点（用于图1 Close Win/Loss）
            pr = float(self.datas[0].close[0])
            self.close_points.append((dt, pr, float(trade.pnlcomm)))
            self.last_exit_bar = self.bar_count


def plot_results(df_plot, strat, initial_cash):
    """
    封装策略回测后的可视化绘图逻辑
    """
    # ---------------- 绘图设置 ----------------
    fig, (ax1, ax2, ax3, ax4, ax5) = plt.subplots(
        5, 1, figsize=(14, 16), sharex=True, gridspec_kw={'height_ratios': [3, 1, 1, 1, 1]}
    )
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
        c_pos_p = [p for _, p, pnl in strat.close_points if pnl >= 0]
        c_neg_dt = [d for d, _, pnl in strat.close_points if pnl < 0]
        c_neg_p = [p for _, p, pnl in strat.close_points if pnl < 0]
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
    for x, y, _ in zip(tp_dates, tp_values, bars):
        ax4.annotate(_fmt_pnl(y), xy=(x, y), xytext=(0, 6 if y >= 0 else -12), textcoords='offset points',
                     ha='center', va='bottom' if y >= 0 else 'top', fontsize=8,
                     color=('green' if y >= 0 else 'red'),
                     bbox=dict(boxstyle='round,pad=0.2', fc='white', alpha=0.8, edgecolor=('green' if y >= 0 else 'red')))
    ax4.axhline(y=0, color='black', linewidth=1.0, alpha=0.5)
    
    ax4.set_ylabel('Trade PnL (USDT)', fontsize=12)
    ax4.legend(loc='upper left', fontsize=8, frameon=True, framealpha=0.8, borderpad=0.3, labelspacing=0.3, handlelength=1.5)
    ax4.grid(True, alpha=0.3)

    # 5. 真实盈亏与资金流
    ax5.plot(strat.dt_records, strat.real_pnl_curve, '-', color='#1f77b4', linewidth=1.5, label='Real PnL')
    ax5.plot(strat.dt_records, strat.withdraw_curve, '--', color='green', linewidth=1.0, alpha=0.8, label='Cum Withdraw')
    ax5.plot(strat.dt_records, strat.external_topup_curve, '--', color='red', linewidth=1.0, alpha=0.8, label='Cum External Topup')
    ax5.axhline(y=0, color='black', linewidth=1.0, alpha=0.5)
    ax5.set_ylabel('Real PnL', fontsize=12)
    ax5.set_xlabel('Time', fontsize=12)
    ax5.legend(loc='upper left', fontsize=8, frameon=True, framealpha=0.8, borderpad=0.3, labelspacing=0.3, handlelength=1.5)
    ax5.grid(True, alpha=0.3)

    # 时间轴格式化
    locator = mdates.AutoDateLocator(minticks=5, maxticks=8)
    formatter = mdates.ConciseDateFormatter(locator)
    ax5.xaxis.set_major_locator(locator)
    ax5.xaxis.set_major_formatter(formatter)
    ax5.set_xlim(x_dt.min(), x_dt.max())
    
    # ---------------- 交互注释与十字线 ----------------
    ann_kw = dict(xy=(0, 0), xytext=(15, 15), textcoords='offset points',
                  bbox=dict(boxstyle='round,pad=0.5', fc='white', alpha=0.9, edgecolor='gray'),
                  arrowprops=dict(arrowstyle='->', alpha=0.5))
    ann1 = ax1.annotate('', **ann_kw)
    ann2 = ax2.annotate('', **ann_kw)
    ann3 = ax3.annotate('', **ann_kw)
    ann4 = ax4.annotate('', **ann_kw)
    ann5 = ax5.annotate('', **ann_kw)
    p_ann1 = ax1.annotate('', **ann_kw)
    p_ann2 = ax2.annotate('', **ann_kw)
    p_ann3 = ax3.annotate('', **ann_kw)
    p_ann4 = ax4.annotate('', **ann_kw)
    p_ann5 = ax5.annotate('', **ann_kw)
    
    for ann in [ann1, ann2, ann3, ann4, ann5, p_ann1, p_ann2, p_ann3, p_ann4, p_ann5]:
        ann.set_visible(False)
    
    vline1 = ax1.axvline(x=x_dt[0], color='black', alpha=0.4, linestyle='--')
    vline2 = ax2.axvline(x=x_dt[0], color='black', alpha=0.4, linestyle='--')
    vline3 = ax3.axvline(x=x_dt[0], color='black', alpha=0.4, linestyle='--')
    vline4 = ax4.axvline(x=x_dt[0], color='black', alpha=0.4, linestyle='--')
    vline5 = ax5.axvline(x=x_dt[0], color='black', alpha=0.4, linestyle='--')
    
    hline1 = ax1.axhline(y=0, color='black', alpha=0.4, linestyle='--')
    hline2 = ax2.axhline(y=0, color='black', alpha=0.4, linestyle='--')
    hline3 = ax3.axhline(y=0, color='black', alpha=0.4, linestyle='--')
    hline4 = ax4.axhline(y=0, color='black', alpha=0.4, linestyle='--')
    hline5 = ax5.axhline(y=0, color='black', alpha=0.4, linestyle='--')
    
    vlines = [vline1, vline2, vline3, vline4, vline5]
    hlines = [hline1, hline2, hline3, hline4, hline5]
    
    p_dot1, = ax1.plot([], [], 'o', color='#1f77b4', markersize=6, alpha=0.9, zorder=6)
    p_dot2, = ax2.plot([], [], 'o', color='purple', markersize=6, alpha=0.9, zorder=6)
    p_dot3, = ax3.plot([], [], 'o', color='#17becf', markersize=6, alpha=0.9, zorder=6)
    p_dot4, = ax4.plot([], [], 'o', color='gray', markersize=6, alpha=0.9, zorder=6)
    p_dot5, = ax5.plot([], [], 'o', color='#1f77b4', markersize=6, alpha=0.9, zorder=6)
    p_dot1.set_visible(False)
    p_dot2.set_visible(False)
    p_dot3.set_visible(False)
    p_dot4.set_visible(False)
    p_dot5.set_visible(False)
    
    for line in vlines + hlines:
        line.set_visible(False)
    
    def on_move(event):
        if not event.inaxes:
            for item in [ann1, ann2, ann3, ann4, ann5] + vlines + hlines:
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
        y_real = float(strat.real_pnl_curve[eq_idx]) if eq_idx >= 0 else 0.0
            
        # 隐藏所有横线和注释
        for h in hlines: h.set_visible(False)
        for a in [ann1, ann2, ann3, ann4, ann5]: a.set_visible(False)
            
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
        elif event.inaxes == ax5:
            ann5.xy = (xi, y_real)
            ann5.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nReal PnL: {y_real:,.2f}")
            ann5.set_visible(True)
            hline5.set_ydata([y_real, y_real])
            hline5.set_visible(True)
            
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
        elif event.inaxes == ax5:
            eq_idx = min(idx, len(strat.real_pnl_curve) - 1)
            y_real = float(strat.real_pnl_curve[eq_idx]) if eq_idx >= 0 else 0.0
            p_ann5.xy = (xi, y_real)
            p_ann5.set_text(f"{x_dt[idx].strftime('%Y-%m-%d %H:%M')}\nReal PnL: {y_real:,.2f}")
            p_ann5.set_visible(True)
            p_dot5.set_data([x_dt[idx]], [y_real])
            p_dot5.set_visible(True)
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
    # 兼容写法：按当前K线收盘价成交（Cheat-On-Close）
    cerebro.broker.set_coc(True)

    # 替换为新的右侧拐点策略
    cerebro.addstrategy(RightSidePivotStrategy)
    # 获取当前运行脚本所在目录  
    modpath = os.path.dirname(os.path.abspath(sys.argv[0]))

    # 用配置读取CSV数据（切换数据源只改 ACTIVE_DATA_SOURCE）
    data_file = DATA_SOURCES.get(ACTIVE_DATA_SOURCE)
    if not data_file:
        raise ValueError(f"无效的数据源键: {ACTIVE_DATA_SOURCE}，可选: {list(DATA_SOURCES.keys())}")
    data_path = os.path.join(modpath, "..", DATA_SOURCE_SUBDIR, data_file)
    df = pd.read_csv(data_path, parse_dates=['datetime'])
    print(f"当前数据源: {ACTIVE_DATA_SOURCE} -> {data_file}")
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
    # 设置初始资金（USDT）
    cerebro.broker.setcash(BACKTEST_INITIAL_CASH)
    
    # 因为加了 10x 杠杆，需要确保券商允许使用保证金(margin)交易，避免现金不足被拒绝
    comminfo = CryptoCommissionInfo()
    cerebro.broker.addcommissioninfo(comminfo)
    cerebro.broker.set_checksubmit(False) # 允许不检查现金是否足够（模拟杠杆借贷）
    
    # 收益率分析器：整体
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='timereturn')
    
    # 引擎运行前打印期出资金  
    initial_cash = cerebro.broker.getvalue()
    print('组合期初资金: %.2f' % initial_cash) 
    results = cerebro.run() 
    strat = results[0]
    # 引擎运行后打期末资金  
    print('组合期末资金: %.2f' % cerebro.broker.getvalue())
    if strat.real_pnl_curve:
        print('累计取出: %.2f' % strat.cum_withdraw)
        print('累计外部补资: %.2f' % strat.cum_topup_external)
        print('后备资金上限: %.2f' % strat.p.max_external_topup)
        print('后备资金是否耗尽: %s' % ('是' if strat.funding_exhausted else '否'))
        print('盈利池余额: %.2f' % strat.profit_pool)
        print('真实累计盈亏: %.2f' % strat.real_pnl_curve[-1])
    stats = strat.get_profit_loss_stats()
    if stats:
        ratio_text = 'inf' if np.isinf(stats['profit_loss_ratio']) else f"{stats['profit_loss_ratio']:.2f}"
        print('盈利笔数: %d' % stats['win_count'])
        print('亏损笔数: %d' % stats['loss_count'])
        print('总盈利: %.2f' % stats['total_win'])
        print('总亏损: %.2f' % stats['total_loss'])
        print('平均单笔盈利: %.2f' % stats['avg_win'])
        print('平均单笔亏损: %.2f' % stats['avg_loss'])
        print('盈亏比: %s' % ratio_text)
    monthly = strat.get_real_monthly_returns()
    if monthly:
        print('月度收益率(平仓口径, %)：')
        for k, v in sorted(monthly.items(), key=lambda x: x[0]):
            print(f'  {k}: {v*100:.2f}%')
    
    # 调用绘图函数
    plot_results(df_plot, strat, initial_cash=initial_cash)

if __name__ == "__main__":
    my_strage()
