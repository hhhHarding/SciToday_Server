<script setup lang="ts">
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ApiError } from "../api";
import { useSessionStore } from "../stores/session";

const token = ref("");
const error = ref("");
const loading = ref(false);
const session = useSessionStore();
const route = useRoute();
const router = useRouter();

async function submit() {
  const value = token.value.trim();
  if (!value) {
    error.value = "请输入租户 Token";
    return;
  }
  loading.value = true;
  error.value = "";
  try {
    await session.login(value);
    token.value = "";
    const target =
      typeof route.query.redirect === "string" && route.query.redirect.startsWith("/")
        ? route.query.redirect
        : "/messages";
    await router.replace(target);
  } catch (reason) {
    if (reason instanceof ApiError && reason.status === 429) {
      error.value = `尝试次数过多，请在 ${reason.retryAfter || 60} 秒后重试`;
    } else {
      error.value = "Token 无效、已撤销或租户已停用";
    }
  } finally {
    token.value = "";
    loading.value = false;
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-card">
      <div class="brand large"><strong>Sci</strong><span>Today</span></div>
      <h1>连接你的科学阅读空间</h1>
      <p>输入运营者发放的租户 Token。验证成功后，浏览器只保留受保护的短期会话。</p>
      <form @submit.prevent="submit">
        <label for="tenant-token">访问 Token</label>
        <input
          id="tenant-token"
          v-model="token"
          type="password"
          autocomplete="off"
          spellcheck="false"
          placeholder="rssai_tk_…"
          :disabled="loading"
        />
        <div v-if="error" class="form-error" role="alert">{{ error }}</div>
        <button class="primary wide" type="submit" :disabled="loading">
          {{ loading ? "正在验证…" : "登录" }}
        </button>
      </form>
      <small>会话最长保留 30 天；Token 撤销后会立即失效。</small>
    </section>
  </main>
</template>
