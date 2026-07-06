import type { Digest } from "./types";

export function displayTitle(digest: Digest): string {
  return digest.cn_title || digest.title || "无标题";
}

export function displayTime(value: string): string {
  const match = value.match(/^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/);
  return match ? `${match[1]}-${match[2]}-${match[3]} ${match[4]}:${match[5]}` : value;
}

export function journalName(digest: Digest): string {
  return digest.journal?.trim() || "未分类";
}

export function preferenceKey(tenant: string, name: string): string {
  return `scitoday:${tenant}:${name}`;
}

export function uploadId(file: File): string {
  const random = crypto.getRandomValues(new Uint32Array(4));
  return Array.from(random, (value) => value.toString(16).padStart(8, "0")).join("");
}
