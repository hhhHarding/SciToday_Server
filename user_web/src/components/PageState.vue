<script setup lang="ts">
import type { TaskProgress } from "../types";

defineProps<{
  error?: string;
  notice?: string;
  progress?: TaskProgress | null;
  progressLabel?: string;
}>();
defineEmits<{ undo: [] }>();
</script>

<template>
  <div v-if="error" class="banner error-banner">{{ error }}</div>
  <div v-if="notice" class="banner notice-banner">
    <span>{{ notice }}</span>
    <button v-if="notice.startsWith('已删除')" type="button" @click="$emit('undo')">撤销</button>
  </div>
  <div v-if="progress" class="progress-card">
    <div>
      <strong>{{ progressLabel || "任务处理中" }}</strong>
      <span>{{ progress.message }}</span>
    </div>
    <progress :max="Math.max(progress.total, 1)" :value="progress.current"></progress>
  </div>
</template>
