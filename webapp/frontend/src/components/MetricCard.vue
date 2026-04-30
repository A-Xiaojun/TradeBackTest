<template>
  <div class="card">
    <div class="label">{{ label }}</div>
    <div class="value">{{ value }}</div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  label: string;
  value: string;
}>();
</script>

<style scoped>
.card {
  background: #141926;
  border: 1px solid #2c3445;
  border-radius: 10px;
  padding: 14px;
}

.label {
  color: #9aa4b2;
  font-size: 12px;
}

.value {
  margin-top: 8px;
  color: #f4f7ff;
  font-size: 20px;
  font-weight: 700;
}
</style>
