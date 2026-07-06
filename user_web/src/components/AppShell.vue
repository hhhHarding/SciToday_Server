<script setup lang="ts">
import { computed } from "vue";
import { useRoute } from "vue-router";
import { useSessionStore } from "../stores/session";

const route = useRoute();
const session = useSessionStore();
const items = [
  { to: "/messages", label: "消息", icon: "✉" },
  { to: "/recommendation", label: "推荐", icon: "★" },
  { to: "/reading", label: "阅读", icon: "▤" },
  { to: "/settings", label: "设置", icon: "⚙" },
];
const detail = computed(() => route.name === "digest");
</script>

<template>
  <div class="app-frame">
    <aside v-if="!detail" class="desktop-rail">
      <div class="brand"><strong>Sci</strong><span>Today</span></div>
      <div class="tenant-name">{{ session.principal?.display_name }}</div>
      <nav aria-label="主导航">
        <RouterLink v-for="item in items" :key="item.to" :to="item.to">
          <span class="nav-icon">{{ item.icon }}</span><span>{{ item.label }}</span>
        </RouterLink>
      </nav>
    </aside>
    <main class="app-content" :class="{ 'detail-content': detail }">
      <RouterView />
    </main>
    <nav v-if="!detail" class="mobile-nav" aria-label="主导航">
      <RouterLink v-for="item in items" :key="item.to" :to="item.to">
        <span class="nav-icon">{{ item.icon }}</span><span>{{ item.label }}</span>
      </RouterLink>
    </nav>
  </div>
</template>
