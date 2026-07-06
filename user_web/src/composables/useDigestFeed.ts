import { onBeforeUnmount, ref } from "vue";
import { api, jsonBody } from "../api";
import type { Digest, ProgressResponse, TaskProgress } from "../types";

export function useDigestFeed(source: "rss" | "pdf", recommendation?: string) {
  const digests = ref<Digest[]>([]);
  const loading = ref(true);
  const refreshing = ref(false);
  const error = ref("");
  const notice = ref("");
  const progress = ref<TaskProgress | null>(null);
  let timer = 0;
  let noticeTimer = 0;

  async function load(showLoading = false) {
    if (showLoading) loading.value = true;
    try {
      const params = new URLSearchParams({ source, limit: "200" });
      if (recommendation) params.set("recommendation", recommendation);
      digests.value = await api<Digest[]>(`/api/digests?${params}`);
      error.value = "";
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : "加载失败";
    } finally {
      loading.value = false;
      refreshing.value = false;
    }
  }

  async function refresh() {
    refreshing.value = true;
    await load();
  }

  function flash(message: string) {
    notice.value = message;
    window.clearTimeout(noticeTimer);
    noticeTimer = window.setTimeout(() => (notice.value = ""), 3000);
  }

  async function trigger() {
    try {
      await api(source === "rss" ? "/api/run/rss" : "/api/run/pdf", {
        method: "POST",
      });
      flash(source === "rss" ? "已启动 RSS 获取" : "已启动 PDF 扫描");
      await poll();
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : "任务启动失败";
    }
  }

  async function poll() {
    try {
      const state = await api<ProgressResponse>("/api/progress");
      const current = state[source];
      const wasActive = Boolean(progress.value?.active);
      progress.value = current.active ? current : null;
      if (wasActive && !current.active) await load();
    } catch {
      // A transient progress failure should not replace already loaded content.
    }
  }

  async function updateFlags(
    digest: Digest,
    patch: Partial<Pick<Digest, "interested" | "disliked" | "is_read">>,
  ) {
    const previous = { ...digest };
    if (patch.interested === true) patch.disliked = false;
    if (patch.disliked === true) patch.interested = false;
    Object.assign(digest, patch);
    try {
      const saved = await api<Pick<Digest, "interested" | "disliked" | "is_read">>(
        `/api/digests/${encodeURIComponent(digest.filename)}/flags`,
        { method: "PATCH", body: jsonBody(patch) },
      );
      Object.assign(digest, saved);
    } catch (reason) {
      Object.assign(digest, previous);
      error.value = reason instanceof Error ? reason.message : "状态更新失败";
    }
  }

  async function remove(digest: Digest) {
    const index = digests.value.findIndex((item) => item.filename === digest.filename);
    if (index < 0) return;
    digests.value.splice(index, 1);
    try {
      await api(`/api/digests/${encodeURIComponent(digest.filename)}`, {
        method: "DELETE",
      });
      flash("已删除，可在 8 秒内撤销");
      window.setTimeout(() => {
        if (notice.value.startsWith("已删除")) notice.value = "";
      }, 8000);
    } catch (reason) {
      digests.value.splice(index, 0, digest);
      error.value = reason instanceof Error ? reason.message : "删除失败";
    }
  }

  async function restore(filename: string) {
    try {
      await api(`/api/digests/${encodeURIComponent(filename)}/restore`, {
        method: "POST",
      });
      await load();
      flash("已撤销删除");
    } catch (reason) {
      error.value = reason instanceof Error ? reason.message : "撤销失败";
    }
  }

  function start() {
    void load(true);
    void poll();
    timer = window.setInterval(poll, 3000);
  }

  function stop() {
    window.clearInterval(timer);
    window.clearTimeout(noticeTimer);
  }

  onBeforeUnmount(stop);
  return {
    digests,
    loading,
    refreshing,
    error,
    notice,
    progress,
    load,
    refresh,
    trigger,
    updateFlags,
    remove,
    restore,
    flash,
    start,
  };
}
