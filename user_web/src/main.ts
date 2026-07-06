import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import { router } from "./router";
import { useSessionStore } from "./stores/session";
import "./styles.css";

const app = createApp(App);
const pinia = createPinia();
app.use(pinia);
app.use(router);

const session = useSessionStore(pinia);
window.addEventListener("scitoday:unauthorized", () => {
  session.$patch({ principal: null, checked: true });
  if (router.currentRoute.value.name !== "login") {
    void router.replace({
      name: "login",
      query: { redirect: router.currentRoute.value.fullPath },
    });
  }
});
router.beforeEach(async (to) => {
  if (!session.checked) await session.restore();
  if (!to.meta.public && !session.authenticated) {
    return { name: "login", query: { redirect: to.fullPath } };
  }
  if (to.name === "login" && session.authenticated) return { name: "messages" };
  return true;
});

app.mount("#app");
