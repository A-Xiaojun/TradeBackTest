<template>
  <div class="wrap">
    <h3>{{ title }}</h3>
    <div ref="chartRef" class="chart"></div>
  </div>
</template>

<script setup lang="ts">
import * as echarts from "echarts";
import { onBeforeUnmount, onMounted, ref, watch } from "vue";

const props = defineProps<{
  title: string;
  xData: string[];
  yData: number[];
  color?: string;
}>();

const chartRef = ref<HTMLDivElement | null>(null);
let chart: echarts.ECharts | null = null;

const render = () => {
  if (!chartRef.value) return;
  chart = chart ?? echarts.init(chartRef.value);
  chart.setOption({
    backgroundColor: "transparent",
    grid: { left: 36, right: 16, top: 30, bottom: 26 },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: props.xData,
      axisLabel: { color: "#9aa4b2", fontSize: 10 },
    },
    yAxis: {
      type: "value",
      axisLabel: { color: "#9aa4b2", fontSize: 10 },
      splitLine: { lineStyle: { color: "#2c3445" } },
    },
    series: [
      {
        type: "line",
        smooth: true,
        showSymbol: false,
        data: props.yData,
        lineStyle: { width: 2, color: props.color ?? "#4f8cff" },
      },
    ],
    tooltip: { trigger: "axis" },
  });
};

onMounted(render);
watch(() => [props.xData, props.yData], render, { deep: true });
onBeforeUnmount(() => chart?.dispose());
</script>

<style scoped>
.wrap {
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

.chart {
  width: 100%;
  height: 280px;
}
</style>
