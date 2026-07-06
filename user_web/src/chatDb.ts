import type { ChatMessage } from "./types";

export interface ChatSession {
  messages: ChatMessage[];
  historySummary: string;
  activePdfName: string;
}

const DB_NAME = "scitoday-user";
const STORE = "chat-sessions";

function openDb(): Promise<IDBDatabase> {
  return new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE)) {
        request.result.createObjectStore(STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

export async function loadChat(key: string): Promise<ChatSession> {
  const db = await openDb();
  return new Promise<ChatSession>((resolve, reject) => {
    const request = db.transaction(STORE, "readonly").objectStore(STORE).get(key);
    request.onsuccess = () =>
      resolve(
        request.result || {
          messages: [],
          historySummary: "",
          activePdfName: "",
        },
      );
    request.onerror = () => reject(request.error);
  }).finally(() => db.close());
}

export async function saveChat(key: string, value: ChatSession): Promise<void> {
  const db = await openDb();
  return new Promise<void>((resolve, reject) => {
    const request = db.transaction(STORE, "readwrite").objectStore(STORE).put(value, key);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  }).finally(() => db.close());
}

export async function clearChat(key: string): Promise<void> {
  const db = await openDb();
  return new Promise<void>((resolve, reject) => {
    const request = db.transaction(STORE, "readwrite").objectStore(STORE).delete(key);
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error);
  }).finally(() => db.close());
}
