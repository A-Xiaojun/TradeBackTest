<template>
  <main class="page">
    <header class="toolbar">
      <h1>策略表现看板</h1>
      <select v-model.number="selectedId" @change="loadAll">
        <option v-for="s in strategies" :key="s.id" :value="s.id">
          {{ s.name }} ({{ s.symbol }})
        </option>
      </select>
    </header>

    <section class="metrics">
      <MetricCard label="累计收益" :value="fmtPct(metrics.total_return)" />
      <MetricCard label="年化收益" :value="fmtPct(metrics.annual_return)" />
      <MetricCard label="最大回撤" :value="fmtPct(metrics.max_drawdown)" />
      <MetricCard label="夏普比率" :value="metrics.sharpe.toFixed(3)" />
      <MetricCard label="胜率" :value="fmtPct(metrics.win_rate)" />
      <MetricCard label="盈亏比" :value="metrics.profit_loss_ratio.toFixed(2)" />
      <MetricCard label="交易次数" :value="String(metrics.trade_count)" />
    </section>

    <section class="charts">
      <SeriesChart title="净值曲线" :x-data="equityX" :y-data="equityY" color="#4f8cff" />
      <SeriesChart title="回撤曲线" :x-data="ddX" :y-data="ddY" color="#ff7a45" />
    </section>

    <TradeTable :items="trades" />
  </main>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import MetricCard from "../components/MetricCard.vue";
import SeriesChart from "../components/SeriesChart.vue";
import TradeTable from "../components/TradeTable.vue";
import { api, type Metric, type Strategy, type TradePoint } from "../api/client";

const strategies = ref<Strategy[]>([]);
const selectedId = ref<number>(0);
const metrics = ref<Metric>({
  strategy_id: 0,
  total_return: 0,
  annual_return: 0,
  max_drawdown: 0,
  sharpe: 0,
  win_rate: 0,
  profit_loss_ratio: 0,
  trade_count: 0,
});
const equityX = ref<string[]>([]);
const equityY = ref<number[]>([]);
const ddX = ref<string[]>([]);
const ddY = ref<number[]>([]);
const trades = ref<TradePoint[]>([]);

const fmtPct = (num: number) => `${(num * 100).toFixed(2)}%`;

const loadAll = async () => {
  if (!selectedId.value) return;
  metrics.value = await api.metrics(selectedId.value);

  const [equity, drawdown, tradePage] = await Promise.all([
    api.equity(selectedId.value),
    api.drawdown(selectedId.value),
    api.trades(selectedId.value),
  ]);
  equityX.value = equity.map((x) => x.ts);
  equityY.value = equity.map((x) => Number(x.equity));
  ddX.value = drawdown.map((x) => x.ts);
  ddY.value = drawdown.map((x) => Number(x.drawdown));
  trades.value = tradePage.items;
};

onMounted(async () => {
  strategies.value = await api.strategies();
  if (strategies.value.length > 0) {
    selectedId.value = strategies.value[0].id;
    await loadAll();
  }
});
</script>

<style scoped>
.page {
  min-height: 100vh;
  background: #0f1420;
  color: #f4f7ff;
  padding: 18px;
  font-family: Arial, sans-serif;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

h1 {
  margin: 0;
  font-size: 20px;
}

select {
  background: #141926;
  color: #f4f7ff;
  border: 1px solid #2c3445;
  border-radius: 8px;
  padding: 8px 10px;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(7, minmax(100px, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}

.charts {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 10px;
  margin-bottom: 12px;
}
</style>
