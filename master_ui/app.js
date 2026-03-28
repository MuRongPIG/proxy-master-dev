const { createApp, nextTick } = Vue;

createApp({
  data() {
    return {
      tabs: [
        { key: "overview", name: "总览", desc: "集群核心指标与趋势图" },
        { key: "proxies", name: "代理池", desc: "代理池查询与过滤" },
        { key: "tasks", name: "任务中心", desc: "任务生命周期与 UUID 跟踪" },
        { key: "nodes", name: "节点中心", desc: "在线节点与 worker 心跳" },
        { key: "ops", name: "运维操作", desc: "导入、分发、清理、重算" },
      ],
      activeTab: "overview",
      cfg: {
        baseUrl: localStorage.getItem("master_ui_base_url") || "http://127.0.0.1:62071",
        nodeToken: localStorage.getItem("master_ui_node_token") || "",
        refreshSeconds: Number(localStorage.getItem("master_ui_refresh_seconds") || "15"),
      },
      filters: {
        proxyStatus: "",
        proxyTier: "",
        proxyLimit: 100,
        taskLimit: 100,
        heartbeatTimeout: 60,
      },
      ops: {
        importProxiesText: "",
        importMaxRetries: 2,
        importUrl: "",
        importUrlRetries: 2,
        importUrlsText: "",
        importUrlsRetries: 2,
        importFile: null,
        importFileRetries: 2,
        cleanupDeadDays: 1,
        cleanupFailThreshold: 5,
      },
      health: { ok: false, text: "未连接" },
      stats: {
        proxy_total: 0,
        proxy_alive: 0,
        proxy_dead: 0,
        task_pending: 0,
        task_assigned: 0,
        task_done: 0,
        task_failed: 0,
      },
      proxies: [],
      tasks: [],
      nodes: [],
      proxyPage: 1,
      taskPage: 1,
      proxyHasMore: false,
      taskHasMore: false,
      logs: [],
      autoTimer: null,
      tierChart: null,
      taskChart: null,
    };
  },
  computed: {
    currentTab() {
      return this.tabs.find((x) => x.key === this.activeTab) || this.tabs[0];
    },
    aliveRateText() {
      const alive = Number(this.stats.proxy_alive || 0);
      const dead = Number(this.stats.proxy_dead || 0);
      const denominator = alive + dead;
      if (denominator <= 0) {
        return "0.00%";
      }
      const rate = (alive / denominator) * 100;
      return `${rate.toFixed(2)}%`;
    },
    statItems() {
      return [
        { key: "proxy_total", label: "代理总数", value: this.stats.proxy_total },
        { key: "proxy_alive", label: "存活代理", value: this.stats.proxy_alive },
        { key: "proxy_dead", label: "失效代理", value: this.stats.proxy_dead },
        { key: "proxy_alive_rate", label: "存活率", value: this.aliveRateText },
        { key: "task_pending", label: "待处理任务", value: this.stats.task_pending },
        { key: "task_assigned", label: "已分配任务", value: this.stats.task_assigned },
        { key: "task_done", label: "完成任务", value: this.stats.task_done },
        { key: "task_failed", label: "失败任务", value: this.stats.task_failed },
      ];
    },
    proxiesPaginationEnabled() {
      return Number(this.filters.proxyLimit || 0) > 0;
    },
    tasksPaginationEnabled() {
      return Number(this.filters.taskLimit || 0) > 0;
    },
  },
  methods: {
    nowText() {
      return new Date().toLocaleString();
    },
    log(title, payload) {
      this.logs.unshift(`[${this.nowText()}] ${title}\n${JSON.stringify(payload, null, 2)}`);
      this.logs = this.logs.slice(0, 80);
    },
    linesToArray(text) {
      return String(text || "")
        .split("\n")
        .map((x) => x.trim())
        .filter((x) => x.length > 0);
    },
    buildUrl(path, query) {
      const url = new URL(this.cfg.baseUrl.replace(/\/$/, "") + path);
      Object.entries(query || {}).forEach(([k, v]) => {
        if (v === undefined || v === null || v === "") {
          return;
        }
        url.searchParams.set(k, String(v));
      });
      return url.toString();
    },
    async api(method, path, { query = null, body = null, auth = false, formData = null } = {}) {
      const headers = {};
      if (auth) {
        if (!this.cfg.nodeToken) {
          throw new Error("缺少节点令牌");
        }
        headers["X-Node-Token"] = this.cfg.nodeToken;
      }
      if (!formData && body !== null) {
        headers["Content-Type"] = "application/json";
      }
      const res = await fetch(this.buildUrl(path, query), {
        method,
        headers,
        body: formData ? formData : body !== null ? JSON.stringify(body) : null,
      });
      const text = await res.text();
      let payload;
      try {
        payload = text ? JSON.parse(text) : null;
      } catch (_e) {
        payload = text;
      }
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${JSON.stringify(payload)}`);
      }
      return payload;
    },
    saveConfig() {
      this.cfg.baseUrl = this.cfg.baseUrl.trim().replace(/\/$/, "");
      this.cfg.refreshSeconds = Math.max(5, Number(this.cfg.refreshSeconds || 15));
      localStorage.setItem("master_ui_base_url", this.cfg.baseUrl);
      localStorage.setItem("master_ui_node_token", this.cfg.nodeToken);
      localStorage.setItem("master_ui_refresh_seconds", String(this.cfg.refreshSeconds));
      this.log("配置已保存", this.cfg);
      this.restartTimer();
    },
    restartTimer() {
      if (this.autoTimer) {
        clearInterval(this.autoTimer);
      }
      this.autoTimer = setInterval(() => this.refreshAll(), Math.max(5, this.cfg.refreshSeconds) * 1000);
    },
    async loadHealth() {
      try {
        await this.api("GET", "/health");
        this.health = { ok: true, text: "连接正常" };
      } catch (err) {
        this.health = { ok: false, text: "连接失败" };
        this.log("健康检查失败", { error: String(err) });
      }
    },
    async loadStats() {
      this.stats = await this.api("GET", "/stats");
    },
    async loadProxies() {
      const limit = Number(this.filters.proxyLimit || 100);
      const page = Math.max(1, Number(this.proxyPage || 1));
      const offset = limit > 0 ? (page - 1) * limit : 0;

      const rows = await this.api("GET", "/proxies", {
        query: {
          status: this.filters.proxyStatus,
          pool_tier: this.filters.proxyTier,
          limit,
          offset,
        },
      });

      this.proxies = rows;
      if (limit > 0) {
        this.proxyHasMore = rows.length === limit;
      } else {
        this.proxyPage = 1;
        this.proxyHasMore = false;
      }
    },
    async loadTasks() {
      const limit = Number(this.filters.taskLimit || 100);
      const page = Math.max(1, Number(this.taskPage || 1));
      const offset = limit > 0 ? (page - 1) * limit : 0;

      const rows = await this.api("GET", "/tasks", {
        query: { limit, offset },
      });

      this.tasks = rows;
      if (limit > 0) {
        this.taskHasMore = rows.length === limit;
      } else {
        this.taskPage = 1;
        this.taskHasMore = false;
      }
    },
    async queryProxies() {
      this.proxyPage = 1;
      await this.loadProxies();
    },
    async queryTasks() {
      this.taskPage = 1;
      await this.loadTasks();
    },
    async prevProxyPage() {
      if (!this.proxiesPaginationEnabled || this.proxyPage <= 1) {
        return;
      }
      this.proxyPage -= 1;
      await this.loadProxies();
    },
    async nextProxyPage() {
      if (!this.proxiesPaginationEnabled || !this.proxyHasMore) {
        return;
      }
      this.proxyPage += 1;
      await this.loadProxies();
    },
    async prevTaskPage() {
      if (!this.tasksPaginationEnabled || this.taskPage <= 1) {
        return;
      }
      this.taskPage -= 1;
      await this.loadTasks();
    },
    async nextTaskPage() {
      if (!this.tasksPaginationEnabled || !this.taskHasMore) {
        return;
      }
      this.taskPage += 1;
      await this.loadTasks();
    },
    async loadNodes() {
      this.nodes = await this.api("GET", "/nodes/online", {
        query: { heartbeat_timeout: Number(this.filters.heartbeatTimeout || 60) },
      });
    },
    async refreshAll() {
      try {
        await this.loadHealth();
        await Promise.all([this.loadStats(), this.loadProxies(), this.loadTasks(), this.loadNodes()]);
        this.log("刷新成功", {
          stats: this.stats,
          proxies: this.proxies.length,
          tasks: this.tasks.length,
          nodes: this.nodes.length,
        });
        await this.$nextTick();
        this.renderCharts();
      } catch (err) {
        this.log("刷新失败", { error: String(err) });
      }
    },
    renderCharts() {
      const tierCount = { excellent: 0, good: 0, bad: 0, unknown: 0 };
      this.proxies.forEach((p) => {
        const key = p.pool_tier || "unknown";
        tierCount[key] = (tierCount[key] || 0) + 1;
      });

      if (!this.tierChart) {
        this.tierChart = echarts.init(document.getElementById("tierChart"));
      }
      this.tierChart.setOption({
        tooltip: { trigger: "item" },
        series: [
          {
            type: "pie",
            radius: ["35%", "68%"],
            data: [
              { name: "excellent", value: tierCount.excellent },
              { name: "good", value: tierCount.good },
              { name: "bad", value: tierCount.bad },
              { name: "unknown", value: tierCount.unknown },
            ],
          },
        ],
      });

      const taskCount = { pending: 0, assigned: 0, done: 0, failed: 0 };
      this.tasks.forEach((t) => {
        const k = t.status || "pending";
        taskCount[k] = (taskCount[k] || 0) + 1;
      });

      if (!this.taskChart) {
        this.taskChart = echarts.init(document.getElementById("taskChart"));
      }
      this.taskChart.setOption({
        tooltip: { trigger: "axis" },
        xAxis: { type: "category", data: ["pending", "assigned", "done", "failed"] },
        yAxis: { type: "value" },
        series: [
          {
            type: "bar",
            data: [taskCount.pending, taskCount.assigned, taskCount.done, taskCount.failed],
            itemStyle: {
              color: "#0b66ff",
              borderRadius: [6, 6, 0, 0],
            },
          },
        ],
      });
    },
    onFileChange(event) {
      const files = event.target.files;
      this.ops.importFile = files && files.length > 0 ? files[0] : null;
    },
    async doAction(title, fn) {
      try {
        const payload = await fn();
        this.log(`${title} 成功`, payload);
        await this.refreshAll();
      } catch (err) {
        this.log(`${title} 失败`, { error: String(err) });
      }
    },
    importJson() {
      return this.doAction("导入代理(JSON)", () =>
        this.api("POST", "/proxies/import", {
          body: {
            proxies: this.linesToArray(this.ops.importProxiesText),
            max_retries: Number(this.ops.importMaxRetries || 2),
          },
        }),
      );
    },
    importUrl() {
      return this.doAction("导入代理(URL)", () =>
        this.api("POST", "/proxies/import/url", {
          body: {
            url: this.ops.importUrl,
            max_retries: Number(this.ops.importUrlRetries || 2),
          },
        }),
      );
    },
    importUrls() {
      return this.doAction("导入代理(多URL)", () =>
        this.api("POST", "/proxies/import/urls", {
          body: {
            urls: this.linesToArray(this.ops.importUrlsText),
            max_retries: Number(this.ops.importUrlsRetries || 2),
          },
        }),
      );
    },
    importFile() {
      return this.doAction("导入代理(文件)", async () => {
        if (!this.ops.importFile) {
          throw new Error("请选择文件");
        }
        const fd = new FormData();
        fd.append("file", this.ops.importFile);
        return this.api("POST", "/proxies/import/file", {
          query: { max_retries: Number(this.ops.importFileRetries || 2) },
          formData: fd,
        });
      });
    },
    dispatch() {
      return this.doAction("分发任务", () => this.api("POST", "/distribution/dispatch"));
    },
    recalc() {
      return this.doAction("重算池分层", () => this.api("POST", "/pool/recalculate"));
    },
    cleanupDead() {
      return this.doAction("清理死代理", () =>
        this.api("POST", "/pool/cleanup", {
          query: { days: Number(this.ops.cleanupDeadDays || 1) },
        }),
      );
    },
    cleanupFailed() {
      return this.doAction("清理失败代理", () =>
        this.api("POST", "/pool/cleanup-failed", {
          query: { fail_threshold: Number(this.ops.cleanupFailThreshold || 5) },
        }),
      );
    },
  },
  async mounted() {
    this.restartTimer();
    await this.refreshAll();
    window.addEventListener("resize", () => {
      if (this.tierChart) {
        this.tierChart.resize();
      }
      if (this.taskChart) {
        this.taskChart.resize();
      }
    });
  },
  beforeUnmount() {
    if (this.autoTimer) {
      clearInterval(this.autoTimer);
    }
    if (this.tierChart) {
      this.tierChart.dispose();
    }
    if (this.taskChart) {
      this.taskChart.dispose();
    }
  },
}).mount("#app");
