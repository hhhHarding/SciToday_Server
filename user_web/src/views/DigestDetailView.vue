<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, jsonBody } from "../api";
import { clearChat, loadChat, saveChat } from "../chatDb";
import { useSessionStore } from "../stores/session";
import type { ChatMessage, ChatResponse, DigestContent } from "../types";
import { uploadPdf } from "../upload";

const route = useRoute();
const router = useRouter();
const session = useSessionStore();
const filename = computed(() => String(route.params.filename || ""));
const content = ref<DigestContent | null>(null);
const flags = ref({ interested: false, disliked: false, is_read: true });
const tab = ref<"content" | "pdf" | "chat">("content");
const error = ref("");
const loading = ref(true);
const chatLoading = ref(false);
const attaching = ref(false);
const webSearch = ref(false);
const input = ref("");
const messages = ref<ChatMessage[]>([]);
const historySummary = ref("");
const activePdfName = ref("");
const chatList = ref<HTMLElement | null>(null);
const pdfPicker = ref<HTMLInputElement | null>(null);
const chatKey = computed(() => `${session.principal?.tenant_id || "unknown"}:${filename.value}`);

async function load() {
  loading.value = true;
  try {
    const [detail, savedFlags, savedChat] = await Promise.all([
      api<DigestContent>(`/api/digests/${encodeURIComponent(filename.value)}/content`),
      api<typeof flags.value>(`/api/digests/${encodeURIComponent(filename.value)}/flags`),
      loadChat(chatKey.value),
    ]);
    content.value = detail;
    flags.value = savedFlags;
    messages.value = savedChat.messages;
    historySummary.value = savedChat.historySummary;
    activePdfName.value = savedChat.activePdfName;
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "摘要加载失败";
  } finally {
    loading.value = false;
  }
}
async function toggle(kind: "interested" | "disliked") {
  const patch = { [kind]: !flags.value[kind] } as Record<string, boolean>;
  if (patch.interested) patch.disliked = false;
  if (patch.disliked) patch.interested = false;
  try {
    flags.value = await api<typeof flags.value>(
      `/api/digests/${encodeURIComponent(filename.value)}/flags`,
      { method: "PATCH", body: jsonBody(patch) },
    );
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "偏好更新失败";
  }
}
async function persistChat() {
  await saveChat(chatKey.value, {
    messages: messages.value,
    historySummary: historySummary.value,
    activePdfName: activePdfName.value,
  });
}
async function send(message = input.value.trim(), pdfFilename = "") {
  if (!message || chatLoading.value) return;
  input.value = "";
  messages.value.push({ role: "user", content: message });
  chatLoading.value = true;
  await nextTick();
  chatList.value?.scrollTo({ top: chatList.value.scrollHeight, behavior: "smooth" });
  try {
    const response = await api<ChatResponse>("/api/chat", {
      method: "POST",
      body: jsonBody({
        filename: filename.value,
        message,
        history: messages.value.slice(0, -1),
        history_summary: historySummary.value,
        web_search: webSearch.value,
        pdf_filename: pdfFilename || activePdfName.value,
      }),
    });
    messages.value.push({
      role: "assistant",
      content: response.reply || response.error || "没有收到回答",
    });
    historySummary.value = response.history_summary || historySummary.value;
    await persistChat();
  } catch (reason) {
    messages.value.push({
      role: "assistant",
      content: reason instanceof Error ? reason.message : "提问失败",
    });
  } finally {
    chatLoading.value = false;
    await nextTick();
    chatList.value?.scrollTo({ top: chatList.value.scrollHeight, behavior: "smooth" });
  }
}
async function attachPdf(files: FileList | null) {
  const file = files?.[0];
  if (!file) return;
  attaching.value = true;
  try {
    activePdfName.value = await uploadPdf(file);
    await persistChat();
    await send(`已发送 PDF：${file.name}。请阅读并概括其核心内容。`, activePdfName.value);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "PDF 发送失败";
  } finally {
    attaching.value = false;
    if (pdfPicker.value) pdfPicker.value.value = "";
  }
}
async function resetChat() {
  await clearChat(chatKey.value);
  messages.value = [];
  historySummary.value = "";
  activePdfName.value = "";
}
onMounted(load);
</script>

<template>
  <section class="detail-page">
    <header class="detail-header">
      <button class="back-button" type="button" aria-label="返回" @click="router.back()">←</button>
      <div><h1>{{ content?.title || "论文总结" }}</h1><p>{{ content?.created_at }}</p></div>
      <div class="detail-actions">
        <button :class="{ active: flags.interested }" type="button" @click="toggle('interested')">★</button>
        <button :class="{ active: flags.disliked }" type="button" @click="toggle('disliked')">⊘</button>
      </div>
    </header>
    <div v-if="content?.source === 'pdf'" class="detail-tabs">
      <button :class="{ active: tab === 'content' }" type="button" @click="tab = 'content'">文章内容</button>
      <button :class="{ active: tab === 'pdf' }" type="button" :disabled="!content.pdf_available" @click="tab = 'pdf'">PDF原文</button>
      <button :class="{ active: tab === 'chat' }" type="button" @click="tab = 'chat'">提问</button>
    </div>
    <div v-if="error" class="banner error-banner">{{ error }}</div>
    <div v-if="loading" class="detail-loading"><div class="spinner"></div></div>
    <article v-else-if="tab === 'content' && content" class="article-content">
      <div class="article-meta">
        <span>{{ content.source === "pdf" ? "PDF 总结" : "RSS 总结" }}</span>
        <a v-if="content.original_url" :href="content.original_url" target="_blank" rel="noopener noreferrer">打开原文 ↗</a>
      </div>
      <h2>{{ content.title }}</h2>
      <div class="article-text">{{ content.content }}</div>
    </article>
    <div v-else-if="tab === 'pdf'" class="pdf-pane">
      <iframe :src="`/api/pdf?filename=${encodeURIComponent(filename)}`" title="PDF 原文"></iframe>
    </div>
    <div v-else-if="tab === 'chat'" class="chat-pane">
      <div ref="chatList" class="chat-list">
        <div v-if="!messages.length" class="empty-state">
          <strong>向 AI 追问这篇文章</strong>
          <span>例如：研究方法、主要结论、创新点……</span>
        </div>
        <div v-for="(message, index) in messages" :key="index" class="chat-row" :class="message.role">
          <div class="chat-bubble">{{ message.content }}</div>
        </div>
        <div v-if="chatLoading" class="chat-row assistant"><div class="chat-bubble typing">思考中…</div></div>
      </div>
      <div v-if="activePdfName || attaching" class="chat-attachment">
        {{ attaching ? "正在发送 PDF…" : `当前 PDF：${activePdfName}` }}
      </div>
      <form class="chat-composer" @submit.prevent="send()">
        <textarea v-model="input" rows="2" :placeholder="webSearch ? '输入问题，将同时搜索网页…' : '输入追问…'" :disabled="chatLoading || attaching"></textarea>
        <div class="chat-tools">
          <label><input v-model="webSearch" type="checkbox" /> 搜索网页</label>
          <input ref="pdfPicker" class="visually-hidden" type="file" tabindex="-1" aria-hidden="true" accept="application/pdf,.pdf" @change="attachPdf(($event.target as HTMLInputElement).files)" />
          <button type="button" :disabled="chatLoading || attaching" @click="pdfPicker?.click()">发送PDF</button>
          <button type="button" :disabled="chatLoading" @click="resetChat">清空历史</button>
          <button class="primary" type="submit" :disabled="!input.trim() || chatLoading || attaching">发送</button>
        </div>
      </form>
    </div>
  </section>
</template>
