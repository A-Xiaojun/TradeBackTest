<template>
  <div class="panel">
    <h3>最近交易</h3>
    <table>
      <thead>
        <tr>
          <th>时间</th>
          <th>方向</th>
          <th>动作</th>
          <th>价格</th>
          <th>数量</th>
          <th>PnL</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in items" :key="item.id">
          <td>{{ item.ts }}</td>
          <td>{{ item.side }}</td>
          <td>{{ item.action }}</td>
          <td>{{ item.price }}</td>
          <td>{{ item.qty }}</td>
          <td :class="{ up: item.pnl >= 0, down: item.pnl < 0 }">{{ item.pnl }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
import type { TradePoint } from "../api/client";

defineProps<{
  items: TradePoint[];
}>();
</script>

<style scoped>
.panel {
  background: #141926;
  border: 1px solid #2c3445;
  border-radius: 10px;
  padding: 12px;
}

h3 {
  margin: 0 0 8px;
  font-size: 14px;
  color: #f4f7ff;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

th,
td {
  border-bottom: 1px solid #2c3445;
  text-align: left;
  padding: 8px 4px;
  color: #d7dde8;
}

.up {
  color: #2ecc71;
}

.down {
  color: #ff5f57;
}
</style>
