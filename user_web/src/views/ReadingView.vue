<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import DigestCard from "../components/DigestCard.vue";
import PageState from "../components/PageState.vue";
import { useDigestFeed } from "../composables/useDigestFeed";
import type { Digest } from "../types";
import { uploadPdf } from "../upload";

const router = useRouter();
const feed = useDigestFeed("pdf");
const uploading = ref(false);
const uploadProgress = ref("");
const lastDeleted = ref("");
const picker = ref<HTMLInputElement | null>(null);

async function upload(files: FileList | null) {
  if (!files?.length || uploading.value) return;
  uploading.value = true;
  let completed = 0;
  try {
    for (const file of Array.from(files)) {
      uploadProgress.value = `正在上传 ${file.name}（${completed + 1}/${files.length}）`;
      await uploadPdf(file);
      completed += 1;
    }
    feed.flash(`已上传 ${completed} 个 PDF，正在启动扫描`);
    await feed.trigger();
  } catch (reason) {
    feed.error.value = reason instanceof Error ? reason.message : "PDF 上传失败";
  } finally {
    uploading.value = false;
    uploadProgress.value = "";
    if (picker.value) picker.value.value = "";
  }
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
      <div><h1>阅读</h1><p>PDF 总结 · {{ feed.digests.value.length }} 篇</p></div>
      <div class="header-actions">
        <input
          ref="picker"
          class="visually-hidden"
          type="file"
          tabindex="-1"
          aria-hidden="true"
          accept="application/pdf,.pdf"
          multiple
          @change="upload(($event.target as HTMLInputElement).files)"
        />
        <button type="button" :disabled="uploading" @click="picker?.click()">上传 PDF</button>
        <button class="primary" type="button" :disabled="uploading" @click="feed.trigger">扫描</button>
      </div>
    </header>
    <div v-if="uploading" class="progress-card">
      <div><strong>PDF 上传中</strong><span>{{ uploadProgress }}</span></div>
      <progress></progress>
    </div>
    <PageState
      :error="feed.error.value"
      :notice="feed.notice.value"
      :progress="feed.progress.value"
      progress-label="PDF 处理中"
      @undo="feed.restore(lastDeleted)"
    />
    <div v-if="feed.loading.value" class="skeleton-list"><div v-for="i in 5" :key="i" class="skeleton-card"></div></div>
    <div v-else-if="!feed.digests.value.length" class="empty-state"><strong>暂无 PDF 总结</strong><span>上传 PDF 后启动扫描</span></div>
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
