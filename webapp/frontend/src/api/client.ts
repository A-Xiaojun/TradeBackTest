const BASE_URL = "http://127.0.0.1:8000/api/v1";

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export interface Strategy {
  id: number;
  name: string;
  exchange: string;
  symbol: string;
  timeframe: string;
  status: string;
}

export interface Metric {
  strategy_id: number;
  total_return: number;
  annual_return: number;
  max_drawdown: number;
  sharpe: number;
  win_rate: number;
  profit_loss_ratio: number;
  trade_count: number;
}

export interface EquityPoint {
  ts: string;
  equity: number;
  pnl: number;
  position: number;
}

export interface DrawdownPoint {
  ts: string;
  drawdown: number;
}

export interface TradePoint {
  id: number;
  trade_id?: string;
  ts: string;
  side: string;
  action: string;
  price: number;
  qty: number;
  fee: number;
  pnl: number;
  note?: string;
}

export interface TradePage {
  items: TradePoint[];
  total: number;
}

export const api = {
  health: () => request<{ status: string }>("/health"),
  strategies: () => request<Strategy[]>("/strategies"),
  metrics: (strategyId: number) => request<Metric>(`/strategies/${strategyId}/metrics`),
  equity: (strategyId: number) => request<EquityPoint[]>(`/strategies/${strategyId}/equity`),
  drawdown: (strategyId: number) => request<DrawdownPoint[]>(`/strategies/${strategyId}/equity/drawdown`),
  trades: (strategyId: number) => request<TradePage>(`/strategies/${strategyId}/trades?page=1&size=20`),
};
