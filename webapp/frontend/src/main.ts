import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import DashboardView from "./views/DashboardView.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: "/", component: DashboardView }],
});

const app = createApp({
  template: "<router-view />",
});

app.use(router);
app.mount("#app");
