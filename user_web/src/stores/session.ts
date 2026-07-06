import { defineStore } from "pinia";
import { api, loginWithToken } from "../api";
import type { Principal } from "../types";

interface SessionResponse {
  ok: boolean;
  principal: Principal;
}

export const useSessionStore = defineStore("session", {
  state: () => ({
    principal: null as Principal | null,
    checked: false,
  }),
  getters: {
    authenticated: (state) => Boolean(state.principal),
    hasScope: (state) => (scope: string) => Boolean(state.principal?.scopes.includes(scope)),
    canWriteAi(): boolean {
      return this.hasScope("ai_config_write") || this.hasScope("tenant_admin");
    },
    canAdminTenant(): boolean {
      return this.hasScope("tenant_admin");
    },
  },
  actions: {
    async restore() {
      try {
        const response = await api<SessionResponse>("/api/web/session");
        this.principal = response.principal;
      } catch {
        this.principal = null;
      } finally {
        this.checked = true;
      }
    },
    async login(token: string) {
      const response = (await loginWithToken(token)) as SessionResponse;
      this.principal = response.principal;
      this.checked = true;
    },
    async logout() {
      try {
        await api("/api/web/session", { method: "DELETE" });
      } finally {
        this.principal = null;
        this.checked = true;
      }
    },
  },
});
