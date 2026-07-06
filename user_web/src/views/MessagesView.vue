<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import DigestCard from "../components/DigestCard.vue";
import PageState from "../components/PageState.vue";
import { useDigestFeed } from "../composables/useDigestFeed";
import { useSessionStore } from "../stores/session";
import type { Digest } from "../types";
import { journalName, preferenceKey } from "../utils";

const router = useRouter();
const session = useSessionStore();
const feed = useDigestFeed("rss");
const tenant = session.principal?.tenant_id || "unknown";
const grouped = ref(localStorage.getItem(preferenceKey(tenant, "group-journal")) !== "false");
const collapsed = ref<Set<string>>(
  new Set(JSON.parse(localStorage.getItem(preferenceKey(tenant, "collapsed-journals")) || "[]")),
);
const lastDeleted = ref("");

const groups = computed(() => {
  const result = new Map<string, Digest[]>();
  const interested = feed.digests.value.filter((item) => item.interested);
  if (interested.length) result.set("★ 感兴趣", interested);
  for (const digest of feed.digests.value) {
    const name = journalName(digest);
    const items = result.get(name) || [];
    items.push(digest);
    result.set(name, items);
  }
  return Array.from(result.entries());
});

function toggleGrouping() {
  grouped.value = !grouped.value;
  localStorage.setItem(preferenceKey(tenant, "group-journal"), String(grouped.value));
}
function toggleGroup(name: string) {
  collapsed.value.has(name) ? collapsed.value.delete(name) : collapsed.value.add(name);
  collapsed.value = new Set(collapsed.value);
  localStorage.setItem(
    preferenceKey(tenant, "collapsed-journals"),
    JSON.stringify(Array.from(collapsed.value)),
  );
}
function setAllCollapsed(collapse: boolean) {
  collapsed.value = collapse
    ? new Set(groups.value.map(([name]) => name))
    : new Set();
  localStorage.setItem(
    preferenceKey(tenant, "collapsed-journals"),
    JSON.stringify(Array.from(collapsed.value)),
  );
}
async function open(digest: Digest) {
  if (!digest.is_read) await feed.updateFlags(digest, { is_read: true });
  await router.push(`/digest/${encodeURIComponent(digest.filename)}`);
}
function remove(digest: Digest) {
  lastDeleted.value = digest.filename;
  void feed.remove(digest);
}
function markAllRead() {
  for (const digest of feed.digests.value.filter((item) => !item.is_read)) {
    void feed.updateFlags(digest, { is_read: true });
  }
}
onMounted(feed.start);
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div><h1>订阅</h1><p>{{ grouped ? "按期刊分组" : "最近推送" }} · {{ feed.digests.value.length }} 篇</p></div>
      <div class="header-actions">
        <button type="button" @click="feed.refresh">刷新</button>
        <button class="primary" type="button" @click="feed.trigger">获取最新</button>
        <details class="menu">
          <summary aria-label="更多操作">•••</summary>
          <div>
            <button type="button" @click="toggleGrouping">{{ grouped ? "取消期刊分组" : "按期刊分组" }}</button>
            <button v-if="grouped" type="button" @click="setAllCollapsed(true)">折叠分组</button>
            <button v-if="grouped" type="button" @click="setAllCollapsed(false)">展开分组</button>
            <button type="button" @click="markAllRead">全部已读</button>
          </div>
        </details>
      </div>
    </header>
    <PageState
      :error="feed.error.value"
      :notice="feed.notice.value"
      :progress="feed.progress.value"
      progress-label="RSS 抓取中"
      @undo="feed.restore(lastDeleted)"
    />
    <div v-if="feed.loading.value" class="skeleton-list"><div v-for="i in 5" :key="i" class="skeleton-card"></div></div>
    <div v-else-if="!feed.digests.value.length" class="empty-state"><strong>暂无消息</strong><span>点击“获取最新”开始抓取订阅内容</span></div>
    <div v-else-if="grouped" class="group-list">
      <section v-for="[name, items] in groups" :key="name" class="digest-group">
        <button class="group-header" type="button" @click="toggleGroup(name)">
          <span>{{ name }}</span><small>{{ items.length }} 篇</small><span>{{ collapsed.has(name) ? "▸" : "▾" }}</span>
        </button>
        <div v-if="!collapsed.has(name)" class="digest-list">
          <DigestCard
            v-for="digest in items"
            :key="`${name}:${digest.filename}`"
            :digest="digest"
            @open="open"
            @flags="feed.updateFlags"
            @remove="remove"
          />
        </div>
      </section>
    </div>
    <div v-else class="digest-list">
      <DigestCard
        v-for="digest in feed.digests.value"
        :key="digest.filename"
        :digest="digest"
        @open="open"
        @flags="feed.updateFlags"
        @remove="remove"
      />
    </div>
  </section>
</template>
