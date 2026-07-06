<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import DigestCard from "../components/DigestCard.vue";
import PageState from "../components/PageState.vue";
import { useDigestFeed } from "../composables/useDigestFeed";
import { useSessionStore } from "../stores/session";
import type { Digest } from "../types";
import { preferenceKey } from "../utils";

type Filter = "all" | "ai" | "explore";
const session = useSessionStore();
const router = useRouter();
const feed = useDigestFeed("rss", "any");
const tenant = session.principal?.tenant_id || "unknown";
const saved = localStorage.getItem(preferenceKey(tenant, "recommendation-filter"));
const filter = ref<Filter>(saved === "ai" || saved === "explore" ? saved : "all");
const lastDeleted = ref("");
const visible = computed(() =>
  filter.value === "all"
    ? feed.digests.value
    : feed.digests.value.filter((item) => item.recommendation_type === filter.value),
);
function setFilter(value: Filter) {
  filter.value = value;
  localStorage.setItem(preferenceKey(tenant, "recommendation-filter"), value);
}
async function open(digest: Digest) {
  if (!digest.is_read) await feed.updateFlags(digest, { is_read: true });
  await router.push(`/digest/${encodeURIComponent(digest.filename)}`);
}
function remove(digest: Digest) {
  lastDeleted.value = digest.filename;
  void feed.remove(digest);
}
onMounted(feed.start);
</script>

<template>
  <section class="page">
    <header class="page-header">
      <div><h1>推荐</h1><p>AI推荐与AI探索 · {{ visible.length }} 篇</p></div>
      <button type="button" @click="feed.refresh">刷新</button>
    </header>
    <div class="segmented" aria-label="推荐筛选">
      <button :class="{ active: filter === 'all' }" type="button" @click="setFilter('all')">全部</button>
      <button :class="{ active: filter === 'ai' }" type="button" @click="setFilter('ai')">AI推荐</button>
      <button :class="{ active: filter === 'explore' }" type="button" @click="setFilter('explore')">AI探索</button>
    </div>
    <PageState
      :error="feed.error.value"
      :notice="feed.notice.value"
      :progress="feed.progress.value"
      progress-label="RSS 抓取中"
      @undo="feed.restore(lastDeleted)"
    />
    <div v-if="feed.loading.value" class="skeleton-list"><div v-for="i in 5" :key="i" class="skeleton-card"></div></div>
    <div v-else-if="!visible.length" class="empty-state">
      <strong>{{ filter === "all" ? "推荐尚未生成" : "暂无该类推荐" }}</strong>
      <span>积累新的感兴趣文章后，系统会逐步生成个性化推荐</span>
    </div>
    <div v-else class="digest-list">
      <DigestCard
        v-for="digest in visible"
        :key="digest.filename"
        :digest="digest"
        @open="open"
        @flags="feed.updateFlags"
        @remove="remove"
      />
    </div>
  </section>
</template>
