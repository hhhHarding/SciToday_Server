<script setup lang="ts">
import { computed } from "vue";
import type { Digest } from "../types";
import { displayTime, displayTitle } from "../utils";

const props = defineProps<{ digest: Digest }>();
const emit = defineEmits<{
  open: [digest: Digest];
  flags: [digest: Digest, patch: Partial<Digest>];
  remove: [digest: Digest];
}>();
const score = computed(() =>
  props.digest.final_score == null ? "" : Math.round(props.digest.final_score),
);
</script>

<template>
  <article class="digest-card" :class="{ unread: !digest.is_read }">
    <button class="card-main" type="button" @click="emit('open', digest)">
      <div class="card-heading">
        <span v-if="!digest.is_read" class="unread-dot" aria-label="未读"></span>
        <h3>{{ displayTitle(digest) }}</h3>
      </div>
      <div class="card-meta">
        <span>{{ displayTime(digest.timestamp) }}</span>
        <span v-if="digest.journal">{{ digest.journal }}</span>
      </div>
      <p>{{ digest.preview || digest.keywords || "暂无预览" }}</p>
      <div class="badges">
        <span v-if="digest.recommendation_type === 'ai'" class="badge blue">AI推荐</span>
        <span v-if="digest.recommendation_type === 'explore'" class="badge green">AI探索</span>
        <span v-if="score !== ''" class="badge">评分 {{ score }}</span>
      </div>
    </button>
    <div class="card-actions">
      <button
        type="button"
        :class="{ active: digest.interested }"
        :aria-pressed="digest.interested"
        @click="emit('flags', digest, { interested: !digest.interested })"
      >★ 感兴趣</button>
      <button
        type="button"
        :class="{ active: digest.disliked }"
        :aria-pressed="digest.disliked"
        @click="emit('flags', digest, { disliked: !digest.disliked })"
      >⊘ 不喜欢</button>
      <button
        type="button"
        @click="emit('flags', digest, { is_read: !digest.is_read })"
      >{{ digest.is_read ? "标为未读" : "标为已读" }}</button>
      <button class="danger-text" type="button" @click="emit('remove', digest)">删除</button>
    </div>
  </article>
</template>
