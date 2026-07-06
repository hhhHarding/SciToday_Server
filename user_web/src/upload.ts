import { api } from "./api";
import type { UploadResponse } from "./types";
import { uploadId } from "./utils";

const CHUNK_BYTES = 8 * 1024 * 1024;

export async function uploadPdf(file: File): Promise<string> {
  if (!file.name.toLowerCase().endsWith(".pdf")) {
    throw new Error(`${file.name} 不是 PDF 文件`);
  }
  if (file.size <= CHUNK_BYTES) {
    const form = new FormData();
    form.append("files", file, file.name);
    const result = await api<UploadResponse>("/api/pdf/upload", {
      method: "POST",
      body: form,
    });
    if (!result.ok || !result.paths[0]) {
      throw new Error(result.errors[0]?.error || "PDF 上传失败");
    }
    return result.paths[0];
  }

  const id = uploadId(file);
  const total = Math.ceil(file.size / CHUNK_BYTES);
  let serverFilename = "";
  for (let index = 0; index < total; index += 1) {
    const form = new FormData();
    form.append(
      "chunk",
      file.slice(index * CHUNK_BYTES, Math.min(file.size, (index + 1) * CHUNK_BYTES)),
      file.name,
    );
    form.append("upload_id", id);
    form.append("filename", file.name);
    form.append("index", String(index));
    form.append("total", String(total));
    const result = await api<UploadResponse>("/api/pdf/upload-chunk", {
      method: "POST",
      body: form,
    });
    if (!result.ok) throw new Error(result.errors[0]?.error || "PDF 分片上传失败");
    if (result.paths[0]) serverFilename = result.paths[0];
  }
  if (!serverFilename) throw new Error("PDF 分片上传未完成");
  return serverFilename;
}
