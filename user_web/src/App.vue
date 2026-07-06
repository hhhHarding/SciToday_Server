<script setup lang="ts">
import { useSessionStore } from "./stores/session";

const session = useSessionStore();
</script>

<template>
  <div v-if="!session.checked" class="splash">
    <div class="brand large"><strong>Sci</strong><span>Today</span></div>
    <div class="spinner" aria-label="正在加载"></div>
  </div>
  <RouterView v-else />
</template>
