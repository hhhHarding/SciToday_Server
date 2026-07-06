import { createRouter, createWebHistory } from "vue-router";
import AppShell from "./components/AppShell.vue";
import LoginView from "./views/LoginView.vue";
import MessagesView from "./views/MessagesView.vue";
import RecommendationView from "./views/RecommendationView.vue";
import ReadingView from "./views/ReadingView.vue";
import DigestDetailView from "./views/DigestDetailView.vue";
import SettingsView from "./views/SettingsView.vue";

export const router = createRouter({
  history: createWebHistory("/user/"),
  routes: [
    { path: "/login", name: "login", component: LoginView, meta: { public: true } },
    {
      path: "/",
      component: AppShell,
      children: [
        { path: "", redirect: "/messages" },
        { path: "messages", name: "messages", component: MessagesView },
        { path: "recommendation", name: "recommendation", component: RecommendationView },
        { path: "reading", name: "reading", component: ReadingView },
        { path: "digest/:filename", name: "digest", component: DigestDetailView },
        { path: "settings", name: "settings", component: SettingsView },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/messages" },
  ],
});
