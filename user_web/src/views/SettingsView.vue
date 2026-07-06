<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { api, jsonBody } from "../api";
import { useSessionStore } from "../stores/session";
import type { Feed } from "../types";

interface Status {
  enabled: boolean;
  feeds_count: number;
  inbox_summaries: number;
  last_run: string;
  pending_papers: number;
  pdf_count: number;
}
interface Config {
  ai?: Record<string, string>;
  rss?: { interest_score_threshold?: number };
  schedule?: {
    rss_interval_minutes?: number;
    pdf_interval_minutes?: number;
    enabled?: boolean;
  };
}

const session = useSessionStore();
const router = useRouter();
const status = ref<Status | null>(null);
const feeds = ref<Feed[]>([]);
const loading = ref(true);
const saving = ref("");
const message = ref("");
const error = ref("");
const opmlFile = ref<HTMLInputElement | null>(null);
let timer = 0;

const schedule = reactive({ rss: 30, pdf: 5, enabled: true });
const recommendation = ref(70);
const aiConfig = reactive({ api_key: "", base_url: "", model: "" });
const prompts = reactive({ system_prompt: "", rss_prompt: "", pdf_prompt: "" });
const newFeed = reactive({ title: "", url: "" });
const canWriteAi = computed(() => session.canWriteAi);
const canAdmin = computed(() => session.canAdminTenant);

function showSuccess(value: string) {
  message.value = value;
  error.value = "";
}
function showError(reason: unknown) {
  error.value = reason instanceof Error ? reason.message : "操作失败";
  message.value = "";
}
async function loadStatus() {
  try {
    status.value = await api<Status>("/api/status");
  } catch {
    // Keep the settings form usable when a background status refresh fails.
  }
}
async function load() {
  loading.value = true;
  try {
    const [config, currentFeeds] = await Promise.all([
      api<Config>("/api/config"),
      api<Feed[]>("/api/feeds"),
      loadStatus(),
    ]);
    feeds.value = currentFeeds;
    schedule.rss = config.schedule?.rss_interval_minutes ?? 30;
    schedule.pdf = config.schedule?.pdf_interval_minutes ?? 5;
    schedule.enabled = config.schedule?.enabled ?? true;
    recommendation.value = config.rss?.interest_score_threshold ?? 70;
    Object.assign(aiConfig, {
      api_key: config.ai?.api_key || "",
      base_url: config.ai?.base_url || "",
      model: config.ai?.model || "",
    });
    Object.assign(prompts, {
      system_prompt: config.ai?.system_prompt || "",
      rss_prompt: config.ai?.rss_prompt || "",
      pdf_prompt: config.ai?.pdf_prompt || "",
    });
  } catch (reason) {
    showError(reason);
  } finally {
    loading.value = false;
  }
}
async function saveSchedule() {
  saving.value = "schedule";
  try {
    await api("/api/settings/schedule", {
      method: "PATCH",
      body: jsonBody({
        rss_interval_minutes: Number(schedule.rss),
        pdf_interval_minutes: Number(schedule.pdf),
        enabled: schedule.enabled,
      }),
    });
    showSuccess("定时配置已保存");
    await loadStatus();
  } catch (reason) {
    showError(reason);
  } finally {
    saving.value = "";
  }
}
async function saveRecommendation() {
  saving.value = "recommendation";
  try {
    await api("/api/settings/recommendation", {
      method: "PATCH",
      body: jsonBody({ interest_score_threshold: Number(recommendation.value) }),
    });
    showSuccess("个性化推荐设置已保存");
  } catch (reason) {
    showError(reason);
  } finally {
    saving.value = "";
  }
}
async function saveAi(test = false) {
  saving.value = test ? "ai-test" : "ai";
  try {
    const result = await api<{ ok?: boolean; message?: string }>(
      test ? "/api/ai-config/test" : "/api/ai-config",
      {
        method: test ? "POST" : "PATCH",
        body: jsonBody(aiConfig),
      },
    );
    showSuccess(result.message || (test ? "AI API 测试成功" : "AI 配置已保存"));
  } catch (reason) {
    showError(reason);
  } finally {
    saving.value = "";
  }
}
async function savePrompts() {
  saving.value = "prompts";
  try {
    await api("/api/config", {
      method: "POST",
      body: jsonBody({ ai: prompts }),
    });
    showSuccess("提示词已保存");
  } catch (reason) {
    showError(reason);
  } finally {
    saving.value = "";
  }
}
async function addFeed() {
  if (!newFeed.url.trim()) return;
  saving.value = "feed";
  try {
    await api("/api/feeds", {
      method: "POST",
      body: jsonBody({ title: newFeed.title, url: newFeed.url }),
    });
    newFeed.title = "";
    newFeed.url = "";
    feeds.value = await api<Feed[]>("/api/feeds");
    showSuccess("RSS 源已添加");
  } catch (reason) {
    showError(reason);
  } finally {
    saving.value = "";
  }
}
async function deleteFeed(feed: Feed) {
  if (!confirm(`删除 RSS 源“${feed.title || feed.url}”？`)) return;
  try {
    await api(`/api/feeds?url=${encodeURIComponent(feed.url)}`, { method: "DELETE" });
    feeds.value = feeds.value.filter((item) => item.url !== feed.url);
    showSuccess("RSS 源已删除");
  } catch (reason) {
    showError(reason);
  }
}
async function importOpml(files: FileList | null) {
  const file = files?.[0];
  if (!file) return;
  const form = new FormData();
  form.append("file", file, file.name);
  try {
    const result = await api<{ count: number }>("/api/feeds/import", {
      method: "POST",
      body: form,
    });
    feeds.value = await api<Feed[]>("/api/feeds");
    showSuccess(`已导入 ${result.count} 个 RSS 源`);
  } catch (reason) {
    showError(reason);
  } finally {
    if (opmlFile.value) opmlFile.value.value = "";
  }
}
async function restoreAll() {
  saving.value = "restore";
  try {
    const deleted = await api<{ items: Array<{ filename: string }> }>("/api/digests/deleted");
    for (const item of deleted.items) {
      await api(`/api/digests/${encodeURIComponent(item.filename)}/restore`, {
        method: "POST",
      });
    }
    showSuccess(deleted.items.length ? `已恢复 ${deleted.items.length} 条摘要` : "没有可恢复的摘要");
  } catch (reason) {
    showError(reason);
  } finally {
    saving.value = "";
  }
}
async function resetPrivateData() {
  if (!confirm("确定删除当前账号的私人摘要数据并重置收录状态吗？此操作不可撤销。")) return;
  saving.value = "reset";
  try {
    const result = await api<{ count: number }>("/api/reset", { method: "POST" });
    showSuccess(`已重置并删除 ${result.count} 条摘要`);
    await loadStatus();
  } catch (reason) {
    showError(reason);
  } finally {
    saving.value = "";
  }
}
async function logout() {
  await session.logout();
  await router.replace("/login");
}
onMounted(() => {
  void load();
  timer = window.setInterval(loadStatus, 10000);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <section class="page settings-page">
    <header class="page-header"><div><h1>设置</h1><p>{{ session.principal?.display_name }} · {{ session.principal?.tenant_id }}</p></div></header>
    <div v-if="message" class="banner notice-banner">{{ message }}</div>
    <div v-if="error" class="banner error-banner">{{ error }}</div>
    <div v-if="loading" class="detail-loading"><div class="spinner"></div></div>
    <template v-else>
      <section class="settings-card status-card">
        <div><span>定时任务</span><strong>{{ status?.enabled ? "运行中" : "已暂停" }}</strong></div>
        <div><span>订阅源</span><strong>{{ status?.feeds_count ?? feeds.length }}</strong></div>
        <div><span>摘要</span><strong>{{ status?.inbox_summaries ?? "—" }}</strong></div>
        <div><span>PDF</span><strong>{{ status?.pdf_count ?? "—" }}</strong></div>
        <p>上次运行：{{ status?.last_run || "从未运行" }}</p>
      </section>

      <details class="settings-card" open>
        <summary>定时任务设置</summary>
        <div class="settings-body">
          <label class="switch-row"><input v-model="schedule.enabled" type="checkbox" />启用定时任务</label>
          <div class="field-grid">
            <label>RSS 间隔（分）<input v-model.number="schedule.rss" type="number" min="1" /></label>
            <label>PDF 间隔（分）<input v-model.number="schedule.pdf" type="number" min="1" /></label>
          </div>
          <button class="primary" type="button" :disabled="saving === 'schedule'" @click="saveSchedule">保存定时配置</button>
        </div>
      </details>

      <details class="settings-card" open>
        <summary>个性化推荐</summary>
        <div class="settings-body">
          <label>兴趣分数阈值（0–100）<input v-model.number="recommendation" type="number" min="0" max="100" step="1" /></label>
          <button class="primary" type="button" :disabled="saving === 'recommendation'" @click="saveRecommendation">保存推荐设置</button>
        </div>
      </details>

      <details v-if="canWriteAi" class="settings-card">
        <summary>AI 配置</summary>
        <div class="settings-body">
          <label>API Key<input v-model="aiConfig.api_key" type="password" autocomplete="off" /></label>
          <label>Base URL<input v-model="aiConfig.base_url" type="url" /></label>
          <label>Model<input v-model="aiConfig.model" /></label>
          <div class="button-row">
            <button class="primary" type="button" :disabled="Boolean(saving)" @click="saveAi(false)">保存 API 配置</button>
            <button type="button" :disabled="Boolean(saving)" @click="saveAi(true)">测试</button>
          </div>
        </div>
      </details>

      <details v-if="canWriteAi" class="settings-card">
        <summary>提示词管理</summary>
        <div class="settings-body">
          <label>System Prompt<textarea v-model="prompts.system_prompt" rows="3"></textarea></label>
          <label>RSS 总结提示词<textarea v-model="prompts.rss_prompt" rows="6"></textarea></label>
          <label>PDF 总结提示词<textarea v-model="prompts.pdf_prompt" rows="6"></textarea></label>
          <button class="primary" type="button" :disabled="saving === 'prompts'" @click="savePrompts">保存提示词</button>
        </div>
      </details>

      <details class="settings-card" open>
        <summary>RSS 源（{{ feeds.length }}）</summary>
        <div class="settings-body">
          <input ref="opmlFile" class="visually-hidden" type="file" tabindex="-1" aria-hidden="true" accept=".opml,.xml,text/xml" @change="importOpml(($event.target as HTMLInputElement).files)" />
          <button type="button" @click="opmlFile?.click()">从 OPML 文件导入</button>
          <form class="feed-form" @submit.prevent="addFeed">
            <input v-model="newFeed.title" placeholder="名称" />
            <input v-model="newFeed.url" type="url" required placeholder="RSS URL" />
            <button class="primary" type="submit" :disabled="saving === 'feed'">添加</button>
          </form>
          <div class="feed-list">
            <div v-for="feed in feeds" :key="feed.url">
              <span><strong>{{ feed.title || "未命名" }}</strong><small>{{ feed.url }}</small></span>
              <button class="danger-text" type="button" @click="deleteFeed(feed)">删除</button>
            </div>
          </div>
        </div>
      </details>

      <section class="settings-card danger-zone">
        <h2>数据与会话</h2>
        <div class="button-row">
          <button type="button" :disabled="saving === 'restore'" @click="restoreAll">撤销全部删除</button>
          <button v-if="canAdmin" class="danger" type="button" :disabled="saving === 'reset'" @click="resetPrivateData">删除私人数据并重置</button>
          <button type="button" @click="logout">退出登录</button>
        </div>
        <small>会话到期时间：{{ session.principal ? new Date(session.principal.expires_at * 1000).toLocaleString() : "—" }}</small>
      </section>
    </template>
  </section>
</template>
