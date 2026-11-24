(() => {
  "use strict";

  const state = {
    user: null,
    csrf: null,
    version: "",
    setupRequired: false,
    authMode: "login",
    dashboard: null,
    alertRules: null,
    chatInfo: null,
    chat: {
      messages: [],
      busy: false,
      lastStats: null,
      totals: { turns: 0, prompt: 0, completion: 0, genMs: 0 },
    },
    discovery: null,
    audit: [],
    settings: null,
    settingsFields: [],
    settingsGroups: [],
    settingsProcess: null,
    sshDefaults: {
      user: "",
      port: null,
      key_path: "",
      key_path_configured: false,
      password_supported: false,
    },
    modelOverview: null,
    modelServices: null,
    modelServicesLoading: false,
    modelServiceBusy: false,
    modelTaskTimer: null,
    modelLogs: [],
    modelRank: "0",
    modelLogSearch: "",
    modelLogsLoading: false,
    modelLogsUpdatedAt: 0,
    view: "overview",
    selectedNodes: new Set(),
    selectedDiscovery: new Set(),
    filters: { search: "", group: "", status: "" },
    drawerNodeId: null,
    historyNodeId: null,
    historyRange: 30,
    historyData: null,
    historyLoading: false,
    clusterHistory: null,
    clusterHistoryLoading: false,
    clusterHistoryLoadedAt: 0,
    confirmAction: null,
    pollBusy: false,
    pollTimer: null,
    discoveryBusy: false,
    scanInterface: "",
  };

  const viewMeta = {
    overview: { eyebrow: "CLUSTER OVERVIEW", title: "集群总览" },
    model: { eyebrow: "MODEL RUNTIME", title: "模型管理" },
    chat: { eyebrow: "MODEL TEST", title: "模型测试" },
    nodes: { eyebrow: "NODE INVENTORY", title: "节点管理" },
    discovery: { eyebrow: "LAN DISCOVERY", title: "局域网发现" },
    alerts: { eyebrow: "ALERT CENTER", title: "告警中心" },
    audit: { eyebrow: "OPERATION AUDIT", title: "操作审计" },
    settings: { eyebrow: "SYSTEM SETTINGS", title: "系统设置" },
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function icon(name, className = "") {
    return `<i data-lucide="${escapeHtml(name)}"${className ? ` class="${className}"` : ""}></i>`;
  }

  function refreshIcons(root = document) {
    if (window.lucide?.createIcons) {
      window.lucide.createIcons({
        attrs: { "stroke-width": 1.8, "aria-hidden": "true" },
        root,
      });
    }
  }

  async function api(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    const init = { method, headers, credentials: "same-origin" };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (!["GET", "HEAD", "OPTIONS"].includes(method) && state.csrf) {
      headers["X-CSRF-Token"] = state.csrf;
    }
    const response = await fetch(path, init);
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith("/api/")) {
        showAuth("login");
      }
      const error = new Error(payload.error || `请求失败 (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function formatNumber(value, digits = 0) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return number.toLocaleString("zh-CN", {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
  }

  function formatTokenMillions(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return `${(number / 1_000_000).toLocaleString("zh-CN", {
      maximumFractionDigits: 2,
      minimumFractionDigits: 2,
    })} M`;
  }

  function formatPercent(value, digits = 1) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number.toFixed(digits)}%` : "—";
  }

  function settingValue(key, fallback = null) {
    const value = state.settings?.[key];
    return value === undefined || value === null || value === "" ? fallback : value;
  }

  function browserTimezone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (_error) {
      return "UTC";
    }
  }

  function displayTimezone() {
    return String(settingValue("ui_timezone", browserTimezone()));
  }

  function defaultGroupName() {
    return String(settingValue("default_group_name", "未分组"));
  }

  function gpuFallbackName() {
    return String(settingValue("ui_gpu_fallback_name", "GPU"));
  }

  function configuredHistoryRanges() {
    const field = state.settingsFields.find((item) => item.key === "ui_history_ranges");
    const minimum = Number(field?.min);
    const maximum = Number(field?.max);
    const values = Array.isArray(settingValue("ui_history_ranges", []))
      ? settingValue("ui_history_ranges", [])
      : [];
    const ranges = values
      .map((value) => Number(value))
      .filter(
        (value) =>
          Number.isInteger(value) &&
          (!Number.isFinite(minimum) || value >= minimum) &&
          (!Number.isFinite(maximum) || value <= maximum),
      );
    if (ranges.length) return Array.from(new Set(ranges)).sort((a, b) => a - b);
    const fallback = [
      Number.isFinite(minimum) ? minimum : null,
      Number(settingValue("ui_history_minutes", NaN)),
    ]
      .filter((value) => Number.isInteger(value) && value > 0)
      .filter(
        (value) =>
          (!Number.isFinite(maximum) || value <= maximum),
      );
    return Array.from(new Set(fallback)).sort((a, b) => a - b);
  }

  function formatHistoryRange(minutes) {
    const value = Math.max(1, Number(minutes) || 1);
    if (value % 1440 === 0) return `${value / 1440} 天`;
    if (value % 60 === 0) return `${value / 60} 小时`;
    return `${value} 分钟`;
  }

  function formatPointInterval(seconds) {
    const value = Math.max(1, Number(seconds) || 1);
    if (value % 3600 === 0) return `${value / 3600} HOUR / POINT`;
    if (value % 60 === 0) return `${value / 60} MIN / POINT`;
    return `${value} SEC / POINT`;
  }

  function gpuLoadLabel(gpu) {
    return gpu?.load_source === "power estimate"
      ? "GPU LOAD · POWER EST."
      : "GPU LOAD";
  }

  function formatBytes(value, suffix = "") {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let current = Math.max(0, number);
    let index = 0;
    while (current >= 1024 && index < units.length - 1) {
      current /= 1024;
      index += 1;
    }
    const digits = index >= 3 ? 2 : index >= 1 ? 1 : 0;
    return `${current.toFixed(digits)} ${units[index]}${suffix}`;
  }

  function formatThroughput(bytesPerSecond) {
    const number = Number(bytesPerSecond);
    if (!Number.isFinite(number)) return "—";
    const bits = Math.max(0, number) * 8;
    const units = ["b/s", "Kb/s", "Mb/s", "Gb/s", "Tb/s"];
    let current = bits;
    let index = 0;
    while (current >= 1000 && index < units.length - 1) {
      current /= 1000;
      index += 1;
    }
    return `${current.toFixed(index >= 3 ? 2 : 1)} ${units[index]}`;
  }

  function formatClock(timestamp) {
    if (!timestamp) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZone: displayTimezone(),
    }).format(new Date(timestamp * 1000));
  }

  function formatMinuteClock(timestamp) {
    if (!timestamp) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
      timeZone: displayTimezone(),
    }).format(new Date(timestamp * 1000));
  }

  function formatDateTime(timestamp) {
    if (!timestamp) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
      timeZone: displayTimezone(),
    }).format(new Date(timestamp * 1000));
  }

  function formatIsoDateTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", {
      timeZone: displayTimezone(),
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(date);
  }

  function formatDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value)) return "—";
    const days = Math.floor(value / 86400);
    const hours = Math.floor((value % 86400) / 3600);
    const minutes = Math.floor((value % 3600) / 60);
    if (days) return `${days} 天 ${hours} 小时`;
    if (hours) return `${hours} 小时 ${minutes} 分`;
    return `${minutes} 分钟`;
  }

  function timeAgo(timestamp) {
    if (!timestamp) return "从未采集";
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - timestamp));
    if (seconds < 10) return "刚刚";
    if (seconds < 60) return `${seconds} 秒前`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
    return `${Math.floor(seconds / 86400)} 天前`;
  }

  function statusLabel(status) {
    return (
      {
        online: "在线",
        offline: "离线",
        paused: "已暂停",
        unknown: "待连接",
      }[status] || "未知"
    );
  }

  function metricClass(value, warning = 85, critical = 95) {
    const number = Number(value);
    if (number >= critical) return "red";
    if (number >= warning) return "amber";
    return "";
  }

  function metricColor(value) {
    const ratio = Math.max(0, Math.min(1, Number(value) / 100 || 0));
    const stops = [
      [0, [69, 209, 122]],
      [0.5, [241, 185, 76]],
      [0.75, [244, 132, 61]],
      [1, [255, 101, 107]],
    ];
    let start = stops[0];
    let end = stops.at(-1);
    for (let index = 1; index < stops.length; index += 1) {
      if (ratio <= stops[index][0]) {
        start = stops[index - 1];
        end = stops[index];
        break;
      }
    }
    const span = Math.max(0.0001, end[0] - start[0]);
    const amount = (ratio - start[0]) / span;
    const channels = start[1].map((channel, index) =>
      Math.round(channel + (end[1][index] - channel) * amount),
    );
    return `rgb(${channels.join(", ")})`;
  }

  function nodeMetrics(node) {
    const metrics = node.latest_metrics || {};
    const resources = metrics.resources || {};
    const gpus = Array.isArray(metrics.gpus) ? metrics.gpus : [];
    const gpuUtil = gpus
      .map((gpu) => Number(gpu.load_percent ?? gpu.utilization))
      .filter(Number.isFinite);
    return {
      metrics,
      resources,
      gpus,
      gpu: gpuUtil.length ? Math.max(...gpuUtil) : Number(metrics.compact?.gpu || 0),
    };
  }

  function showToast(message, type = "success") {
    const root = $("#toastRoot");
    const item = document.createElement("div");
    item.className = `toast ${type === "error" ? "error" : ""}`;
    item.innerHTML = `${icon(type === "error" ? "circle-alert" : "circle-check")}<span>${escapeHtml(message)}</span>`;
    root.appendChild(item);
    refreshIcons(item);
    window.setTimeout(() => {
      item.style.opacity = "0";
      item.style.transform = "translateY(6px)";
      window.setTimeout(() => item.remove(), 180);
    }, 3600);
  }

  function setButtonBusy(button, busy, text = "") {
    if (!button) return;
    if (busy) {
      button.dataset.originalHtml = button.innerHTML;
      button.disabled = true;
      button.innerHTML = `${icon("loader-circle", "spin")}<span>${escapeHtml(text || "处理中")}</span>`;
      refreshIcons(button);
    } else {
      button.disabled = false;
      if (button.dataset.originalHtml) button.innerHTML = button.dataset.originalHtml;
      refreshIcons(button);
    }
  }

  function sshDefaults() {
    const defaults = state.sshDefaults || {};
    const port = Number(defaults.port);
    return {
      user: String(defaults.user || "").trim(),
      port: Number.isInteger(port) && port > 0 ? port : "",
      keyPath: String(defaults.key_path || "").trim(),
      keyPathConfigured: Boolean(defaults.key_path_configured),
      passwordSupported: Boolean(defaults.password_supported),
    };
  }

  function sshAddress(user, host, port) {
    const defaults = sshDefaults();
    const resolvedUser = String(user || defaults.user || "").trim();
    const resolvedPort = Number(port || defaults.port || 0);
    return `${resolvedUser || "未配置用户"}@${host || "—"}${resolvedPort ? `:${resolvedPort}` : ""}`;
  }

  function sshCredentialFields({
    passwordHint = "留空时将复用该节点已保存的密码；没有已保存密码时使用私钥或 SSH Agent。",
    keyPlaceholder = "留空使用中心端 SSH 配置",
    user,
    port,
    keyPath,
    passwordConfigured = false,
    allowClearPassword = false,
  } = {}) {
    const defaults = sshDefaults();
    const resolvedUser = user === undefined ? defaults.user : String(user || "").trim();
    const requestedPort = port === undefined ? defaults.port : Number(port);
    const resolvedPort = Number.isInteger(requestedPort) && requestedPort > 0 ? requestedPort : "";
    const resolvedKeyPath = keyPath === undefined ? defaults.keyPath : String(keyPath || "").trim();
    const keyHint = resolvedKeyPath
      ? "此路径优先于中心端默认私钥。"
      : defaults.keyPathConfigured
        ? "中心端已配置默认私钥，留空即可使用。"
        : "可填写中心节点上的私钥文件路径。";
    const passwordStatus = passwordConfigured
      ? `<span class="credential-state configured">${icon("key-round")}<span>已保存加密密码</span></span>`
      : `<span class="credential-state">${icon("key-round")}<span>未保存密码</span></span>`;
    const clearPassword = allowClearPassword && passwordConfigured
      ? `
        <label class="checkbox-field full">
          <input class="checkbox" name="clear_ssh_password" type="checkbox">
          <span>清除已保存的 SSH 密码</span>
        </label>
      `
      : "";
    return `
      <label class="field"><span>SSH 用户</span><input class="text-input" name="ssh_user" value="${escapeHtml(resolvedUser)}" placeholder="SSH 登录用户" required></label>
      <label class="field"><span>SSH 端口</span><input class="text-input" name="ssh_port" type="number" min="1" max="65535" value="${resolvedPort}" placeholder="默认 22"></label>
      <label class="field full"><span>私钥路径</span><input class="text-input" name="ssh_key_path" value="${escapeHtml(resolvedKeyPath)}" placeholder="${escapeHtml(keyPlaceholder)}"><span class="field-hint">${escapeHtml(keyHint)}</span></label>
      <label class="field full"><span>SSH 密码</span><input class="text-input" name="ssh_password" type="password" autocomplete="new-password" placeholder="${passwordConfigured ? "留空保持当前密码" : "留空则使用私钥认证"}"><span class="field-hint">${escapeHtml(passwordHint)}</span>${passwordStatus}</label>
      ${clearPassword}
    `;
  }

  function applySettingsPayload(payload, resetHistoryDefault = false) {
    if (payload?.settings) state.settings = payload.settings;
    if (Array.isArray(payload?.fields)) state.settingsFields = payload.fields;
    if (Array.isArray(payload?.groups)) state.settingsGroups = payload.groups;
    if (payload?.process) state.settingsProcess = payload.process;
    if (payload?.ssh_defaults) {
      const defaults = payload.ssh_defaults;
      state.sshDefaults = {
        user: String(defaults.user || ""),
        port: Number(defaults.port) || null,
        key_path: String(defaults.key_path || ""),
        key_path_configured: Boolean(defaults.key_path_configured),
        password_supported: Boolean(defaults.password_supported),
      };
    }
    if (state.dashboard && state.settings) {
      state.dashboard.thresholds = Object.fromEntries(
        state.settingsFields
          .filter((field) => field.group === "alerts")
          .map((field) => [field.key, state.settings[field.key]]),
      );
      state.dashboard.dashboard_refresh_seconds = state.settings.dashboard_refresh_seconds;
      state.dashboard.poll_interval = state.settings.poll_interval;
    }
    const configuredDefault = Number(state.settings?.ui_history_minutes);
    if (resetHistoryDefault && Number.isFinite(configuredDefault) && configuredDefault >= 5) {
      state.historyRange = configuredDefault;
    } else if (!configuredHistoryRanges().includes(state.historyRange)) {
      state.historyRange = configuredHistoryRanges()[0];
    }
  }

  async function loadSettings(resetHistoryDefault = false) {
    try {
      const payload = await api("/api/settings");
      applySettingsPayload(payload, resetHistoryDefault);
      if (state.view === "settings" && state.settings) renderCurrentView();
    } catch (error) {
      if (error.status !== 401) {
        showToast(`系统设置加载失败：${error.message}`, "error");
      }
    }
  }

  function configureAuth(mode) {
    state.authMode = mode;
    const setup = mode === "setup";
    $("#authKicker").textContent = setup ? "首次部署" : "集群控制台";
    $("#authTitle").textContent = setup ? "创建管理员账号" : "登录 Spark Grid";
    $("#authSubtitle").textContent = setup
      ? "账号会保存在本机数据库中，密码只保存加盐哈希。"
      : "使用管理员凭据进入监控中心。";
    $("#authSubmitText").textContent = setup ? "创建并进入控制台" : "进入控制台";
    $("#authUsername").autocomplete = setup ? "new-username" : "username";
    $("#authPassword").autocomplete = setup ? "new-password" : "current-password";
    $("#authPasswordConfirm").required = setup;
    $$(".setup-only").forEach((element) => {
      element.hidden = !setup;
    });
    $("#authError").hidden = true;
  }

  function showAuth(mode = "login") {
    $("#bootScreen").hidden = true;
    $("#appShell").hidden = true;
    $("#authScreen").hidden = false;
    if (state.drawerNodeId) closeDrawer();
    closeModal();
    configureAuth(mode);
    refreshIcons();
    window.setTimeout(() => $("#authUsername")?.focus(), 50);
  }

  function showApp() {
    $("#bootScreen").hidden = true;
    $("#authScreen").hidden = true;
    $("#appShell").hidden = false;
    $("#userName").textContent = state.user?.username || "admin";
    $("#userInitial").textContent = (state.user?.username || "A").slice(0, 1).toUpperCase();
    refreshIcons();
    navigate(state.view || "overview", false);
  }

  async function boot() {
    bindEvents();
    try {
      const bootstrap = await api("/api/bootstrap");
      state.version = String(bootstrap.version || "");
      state.setupRequired = bootstrap.setup_required;
      if (bootstrap.setup_required) {
        showAuth("setup");
        return;
      }
      try {
        const session = await api("/api/session");
        state.user = session.user;
        state.csrf = session.csrf_token;
        showApp();
        await Promise.all([loadSettings(true), loadDashboard()]);
        startPolling();
      } catch (error) {
        if (error.status === 401) showAuth("login");
        else throw error;
      }
    } catch (error) {
      $("#bootScreen").innerHTML = `
        <div class="empty-state-icon">${icon("server-off")}</div>
        <strong>无法连接控制面</strong>
        <p>${escapeHtml(error.message)}</p>
        <button class="button secondary" data-action="reload-page">${icon("refresh-cw")}<span>重新加载</span></button>
      `;
      refreshIcons($("#bootScreen"));
    }
  }

  function startPolling() {
    if (state.pollTimer !== null) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    const interval = Math.max(
      2,
      Number(
        state.dashboard?.dashboard_refresh_seconds ||
          settingValue("dashboard_refresh_seconds", 5),
      ) || 5,
    );
    state.pollTimer = window.setTimeout(async () => {
      if (!state.user || document.hidden || state.pollBusy) {
        startPolling();
        return;
      }
      state.pollBusy = true;
      try {
        await loadDashboard(true);
        const clusterHistoryRefresh =
          Math.max(
            10,
            Number(
              settingValue("ui_cluster_history_refresh_seconds", 60),
            ) || 60,
          ) * 1000;
        if (Date.now() - state.clusterHistoryLoadedAt > clusterHistoryRefresh) {
          await loadClusterHistory(state.historyRange);
        }
        if (state.view === "discovery") await loadDiscovery(true);
        if (state.view === "audit") await loadAudit(true);
        if (state.view === "model") {
          await loadModelOverview(true);
          await loadModelServices(true);
          const logRefresh =
            Math.max(
              2,
              Number(settingValue("ui_model_log_refresh_seconds", 10)) || 10,
            ) * 1000;
          if (Date.now() - (state.modelLogsUpdatedAt || 0) > logRefresh) {
            await loadModelLogs(true);
          }
        }
      } finally {
        state.pollBusy = false;
        startPolling();
      }
    }, interval * 1000);
  }

  async function loadDashboard(silent = false) {
    try {
      const dashboard = await api("/api/dashboard");
      const hadDashboard = Boolean(state.dashboard);
      state.dashboard = dashboard;
      updateChrome();
      if (!silent || !hadDashboard || !$("#content").children.length) {
        renderCurrentView();
      } else {
        patchCurrentView();
      }
      if (state.drawerNodeId) {
        const node = findNode(state.drawerNodeId);
        if (node) {
          if ($(".drawer")) patchDrawer(node);
          else renderDrawer(node);
        }
      }
      if (!state.clusterHistory && !state.clusterHistoryLoading) {
        loadClusterHistory(state.historyRange);
      }
    } catch (error) {
      if (error.status !== 401) showToast(error.message, "error");
    }
  }

  function patchCurrentView() {
    if (!state.dashboard) return;
    if (state.view === "overview") patchOverview();
    else if (state.view === "nodes") patchNodes();
    else if (state.view === "alerts") patchAlerts();
  }

  async function loadClusterHistory(minutes = state.historyRange) {
    if (state.clusterHistoryLoading) return;
    state.clusterHistoryLoading = true;
    try {
      const payload = await api(`/api/history/cluster?minutes=${minutes}`);
      state.clusterHistory = payload.series || {};
      state.clusterHistoryLoadedAt = Date.now();
      if (state.view === "overview") requestAnimationFrame(drawAllMiniCharts);
    } catch (error) {
      if (error.status !== 401) showToast(error.message, "error");
    } finally {
      state.clusterHistoryLoading = false;
    }
  }

  function updateChrome() {
    const summary = state.dashboard?.summary;
    if (!summary) return;
    $("#sideOnline").textContent = `${summary.online} / ${summary.total}`;
    $("#lastUpdate").textContent = `同步于 ${formatClock(state.dashboard.generated_at)}`;
    const alertBadge = $("#alertNavCount");
    if (alertBadge) {
      const count = state.dashboard.alerts?.length || 0;
      alertBadge.hidden = count === 0;
      alertBadge.textContent = String(count);
    }
    $$(".cluster-mini span").forEach((dot, index) => {
      dot.style.background =
        index < summary.online && index < Math.max(summary.total, 1) ? "var(--green)" : "#59615d";
    });
  }

  function navigate(view, loadData = true) {
    if (!viewMeta[view]) view = "overview";
    state.view = view;
    $$(".nav-button").forEach((button) => {
      button.classList.toggle("active", button.dataset.view === view);
    });
    $$(".runtime-tab").forEach((button) => {
      button.classList.toggle("active", button.dataset.view === view);
    });
    document.title = `${viewMeta[view].title} · Spark Grid`;
    $(".sidebar")?.classList.remove("open");
    renderCurrentView();
    if (loadData) {
      if (view === "discovery") loadDiscovery();
      if (view === "alerts") loadAlertRules();
      if (view === "chat") loadChatInfo();
      if (view === "audit") loadAudit();
      if (view === "model") {
        loadModelOverview();
        loadModelServices();
        loadModelLogs();
      }
    }
  }

  function renderCurrentView() {
    const content = $("#content");
    const settingsReady = state.view === "settings" && state.settings;
    if (!state.dashboard && state.view !== "audit" && !settingsReady) {
      content.innerHTML = loadingView();
      return;
    }
    switch (state.view) {
      case "nodes":
        renderNodes();
        break;
      case "discovery":
        renderDiscovery();
        break;
      case "alerts":
        renderAlerts();
        break;
      case "audit":
        renderAudit();
        break;
      case "model":
        renderModel();
        break;
      case "chat":
        renderChat();
        break;
      case "settings":
        renderSettings();
        break;
      default:
        renderOverview();
    }
    refreshIcons(content);
  }

  function loadingView() {
    return `
      <div class="overview-summary">
        ${Array.from({ length: 5 }, () => '<div class="summary-card skeleton"></div>').join("")}
      </div>
      <div class="node-grid">
        ${Array.from({ length: 8 }, () => '<div class="node-card skeleton"></div>').join("")}
      </div>
    `;
  }

  const HELP_TOPICS = {
    overview: {
      title: "集群总览说明",
      html: `<p>总览页由中心端通过 SSH 主动拉取节点指标：<strong>无常驻 Agent</strong>，节点也不需要额外开放监控端口。</p>
        <ul>
          <li><strong>在线判定</strong>：连续采集失败达到阈值即标记离线，恢复后自动转回在线。</li>
          <li><strong>指标口径</strong>：CPU / 内存 / GPU 均取自节点本地采样；GPU 默认使用驱动利用率，个别型号驱动值不可信时可改用功耗估算。</li>
          <li><strong>采样间隔</strong>：可在系统设置中调整，页面标题处显示当前间隔，右上角显示最近同步时间。</li>
        </ul>`,
    },
    nodes: {
      title: "节点管理说明",
      html: `<p>节点管理维护被监控资产清单，是采集、告警和模型监控的基础。</p>
        <ul>
          <li><strong>SSH 凭据</strong>（私钥或密码）加密保存在中心端，仅用于采集时登录节点。</li>
          <li><strong>分组与标签</strong>用于总览筛选和批量操作。</li>
          <li><strong>健康状态</strong>取决于最近一次 SSH 采集结果，可手动刷新或测试连接。</li>
        </ul>`,
    },
    discovery: {
      title: "局域网发现说明",
      html: `<p>发现功能基于中心节点所在网卡扫描 IPv4 网段，再逐个探测 SSH 端口与主机身份。</p>
        <ul>
          <li><strong>只发现不纳管</strong>：勾选并导入后才会进入节点列表。</li>
          <li><strong>只读探测</strong>：扫描不修改目标主机，仅做端口与身份识别。</li>
          <li>支持选择网卡自动取网段，或手动填写多个 CIDR。</li>
        </ul>`,
    },
    alerts: {
      title: "告警中心说明",
      html: `<p>告警由中心端按阈值规则生成，覆盖 CPU、GPU、内存、温度、离线与心跳。</p>
        <ul>
          <li><strong>分级</strong>：按超出阈值的幅度分为严重 / 警告两级。</li>
          <li><strong>阈值</strong>：可在系统设置中调整，保存后下一轮采集生效。</li>
          <li>本页只展示当前活动告警，历史操作记录见"操作审计"。</li>
        </ul>`,
    },
    "alert-rules": {
      title: "告警规则说明",
      html: `<p>这里展示当前生效的全部告警阈值，可直接修改并保存，无需进入系统设置。</p>
        <ul>
          <li><strong>CPU / 内存 / GPU</strong>：指标达到阈值即产生"警告"级告警。</li>
          <li><strong>温度</strong>：达到阈值产生"严重"级告警。</li>
          <li><strong>离线判定</strong>：最后采集时间超过该秒数即判定节点离线，产生严重告警。</li>
          <li>保存后下一轮采集立即生效；与"系统设置 → 告警阈值"是同一份配置，两处修改互相同步。</li>
        </ul>
        <p class="model-help-note">阈值过松会漏报，过紧会产生噪音；建议按集群日常负载留 10-15% 余量。</p>`,
    },
    audit: {
      title: "操作审计说明",
      html: `<p>记录管理员在平台上的关键操作：登录、节点增删改、批量纳管、扫描、设置变更、模型服务启停与切换。</p>
        <ul>
          <li>每条记录包含<strong>操作人、目标、时间与来源 IP</strong>。</li>
          <li>只读展示，不支持在页面内修改或删除。</li>
        </ul>`,
    },
    settings: {
      title: "系统设置说明",
      html: `<p>设置保存后立即写入，采集类参数在下一轮采样时应用。</p>
        <ul>
          <li><strong>GPU 查询命令</strong>留空时不做 GPU 采样（适用于无 GPU 或无需 GPU 监控的集群）。</li>
          <li><strong>SSH 设置</strong>影响后续所有连接，包括端口、超时与已知主机校验策略。</li>
          <li>修改前建议确认当前采集状态，避免误改导致节点集体离线。</li>
        </ul>`,
    },
    "model-overview": {
      title: "推理服务运行态说明",
      html: `<p>本页展示正在运行的模型服务的<strong>容器级运行态</strong>，引擎（vLLM / SGLang）自动识别。</p>
        <ul>
          <li>指标来自容器进程参数、推理服务 <code>/metrics</code> 与运行日志解析。</li>
          <li><strong>启动时间</strong>取容器 StartedAt；累计请求 / token 为引擎启动后的计数。</li>
          <li><strong>节点健康</strong>为逐节点容器状态 + <code>/health</code> 探测结果。</li>
        </ul>`,
    },
    "model-services": {
      title: "模型服务识别与启停说明",
      html: `<p>Spark Grid 会自动扫描 HEAD 节点上 systemd 中名为 <code>model_*.service</code> 的服务单元，把它们识别为可管理的模型服务，不需要在页面里逐条登记。</p>
        <ul>
          <li><strong>启动 / 停止</strong>：通过 <code>systemctl start / stop</code> 执行，与在服务器上手工操作完全一致；模型服务为 <code>Type=oneshot</code>，命令会等待启动脚本跑完。</li>
          <li><strong>切换到此</strong>：先停掉其他正在运行的模型服务，以及可能把旧模型重新拉起的 guard 单元，再启动目标服务；完成后自动恢复与该模型对应的 guard。</li>
          <li><strong>后台执行</strong>：8 节点启动通常需要数分钟（最长可达 70 分钟），任务在后台运行，本面板每 4 秒自动刷新进度；同一时间只允许一个任务。</li>
          <li><strong>可审计</strong>：每次启停 / 切换都会写入"操作审计"，包含操作人与目标服务。</li>
        </ul>
        <p class="model-help-note">服务状态直接读取 systemd（active / inactive / activating 等），可与顶部"推理服务运行态"的容器级监控相互印证。</p>`,
    },
    "model-params": {
      title: "关键启动参数说明",
      html: `<p>参数从 HEAD 节点容器进程命令行解析（<code>--key value</code> 形式），只展示常用项。</p>
        <ul>
          <li>显示 <strong>—</strong> 表示当前启动命令未显式携带该参数，使用引擎默认值。</li>
          <li>修改参数需要重启模型服务，可在"模型服务控制"面板启动 / 停止 / 切换。</li>
        </ul>`,
    },
    "model-cluster": {
      title: "集群状态说明",
      html: `<p>与总览页同源，按当前模型所在节点的实时采样汇总。</p>
        <ul>
          <li><strong>GPU 负载</strong>为驱动利用率，<strong>统一内存</strong>为 GB10 统一内存占用。</li>
          <li><strong>网络吞吐</strong>为节点网卡收发速率合计。</li>
        </ul>`,
    },
    chat: {
      title: "模型测试说明",
      html: `<p>通过 OpenAI 兼容接口直接和运行中的模型对话，用于验证服务可用性与生成性能。</p>
        <ul>
          <li><strong>输入 / 输出 Token</strong>：取自引擎返回的 usage（<code>stream_options.include_usage</code>）。</li>
          <li><strong>TTFT</strong>：从发出请求到收到第一个内容 token 的时间，反映 prefill 速度。</li>
          <li><strong>吞吐</strong>：输出 token 数除以首 token 之后的生成耗时（tok/s）。</li>
          <li>请求由中心服务转发到 HEAD 节点的模型端口，API Key 从容器 secret 读取，不会出现在页面或日志中。</li>
        </ul>`,
    },
    "model-logs": {
      title: "模型日志说明",
      html: `<p>日志来自当前模型容器的输出（docker logs）。</p>
        <ul>
          <li>可切换 <strong>rank</strong> 查看对应节点，默认 rank0（HEAD）。</li>
          <li>支持关键词筛选与下载，行数上限可在系统设置中调整。</li>
        </ul>`,
    },
  };

  function helpButton(key) {
    if (!HELP_TOPICS[key]) return "";
    return `<button class="model-help-btn" data-action="open-help" data-help="${escapeHtml(key)}" title="说明" aria-label="说明">?</button>`;
  }

  function openHelp(key) {
    const topic = HELP_TOPICS[key];
    if (!topic) return;
    openModal(`
      <div class="modal model-help-modal">
        <div class="modal-head"><h3>${escapeHtml(topic.title)}</h3><button class="icon-button subtle" data-action="close-modal" title="关闭" aria-label="关闭">${icon("x")}</button></div>
        <div class="modal-body model-help-body">${topic.html}</div>
        <div class="modal-foot"><button class="button secondary" data-action="close-modal">知道了</button></div>
      </div>
    `);
  }

  function pageHead(title, subtitle, actions = "", helpKey = "") {
    return `
      <div class="page-head">
        <div class="page-head-copy">
          <h2>${escapeHtml(title)}${helpKey ? ` ${helpButton(helpKey)}` : ""}</h2>
          <p>${escapeHtml(subtitle)}</p>
        </div>
        <div class="page-actions">${actions}</div>
      </div>
    `;
  }

  function summaryCard(key, label, iconName, value, unit, percent, tone = "", detail = "") {
    return `
      <article class="summary-card ${tone === "primary" ? "primary" : ""}" data-summary-key="${key}">
        <div class="summary-label">${icon(iconName)}<span>${escapeHtml(label)}</span></div>
        <div class="summary-value"><strong data-summary-value>${escapeHtml(value)}</strong><span data-summary-unit>${escapeHtml(unit)}</span></div>
        <div class="summary-meter"><i data-summary-meter class="${tone && tone !== "primary" ? tone : ""}" style="width:${Math.max(0, Math.min(100, Number(percent) || 0))}%"></i></div>
        <div class="summary-detail" data-summary-detail ${detail ? "" : "hidden"}>${escapeHtml(detail)}</div>
      </article>
    `;
  }

  function summaryCardDefinitions(summary, thresholds) {
    const loadPercent = summary.cpu_count_avg
      ? (summary.load_1_avg / summary.cpu_count_avg) * 100
      : 0;
    return [
      {
        key: "online",
        label: "在线节点",
        icon: "server",
        value: `${summary.online}/${summary.total}`,
        unit: "ONLINE",
        percent: summary.total ? (summary.online / summary.total) * 100 : 0,
        tone: "primary",
        detail: `离线 ${summary.offline} · 暂停 ${summary.paused}`,
      },
      {
        key: "load",
        label: "系统负载",
        icon: "activity",
        value: summary.load_1_avg.toFixed(2),
        unit: "1 分钟",
        percent: loadPercent,
        tone: metricClass(loadPercent, 70, 90),
        detail: `${formatNumber(summary.cpu_count_avg, 0)} 核平均`,
      },
      {
        key: "cpu",
        label: "CPU 利用率",
        icon: "cpu",
        value: summary.cpu_avg.toFixed(1),
        unit: "% 全核",
        percent: summary.cpu_avg,
        tone: metricClass(summary.cpu_avg, thresholds.cpu_warning),
        detail: "全部核心平均",
      },
      {
        key: "gpu",
        label: "GPU 负载",
        icon: "gauge",
        value: summary.gpu_avg.toFixed(1),
        unit: "% 估算",
        percent: summary.gpu_avg,
        tone: metricClass(summary.gpu_avg, thresholds.gpu_warning),
        detail: `温度 ${formatNumber(summary.temperature_max, 0)}°C`,
      },
      {
        key: "memory",
        label: "统一内存",
        icon: "memory-stick",
        value: summary.memory_avg.toFixed(1),
        unit: "% 已用",
        percent: summary.memory_avg,
        tone: metricClass(summary.memory_avg, thresholds.memory_warning),
        detail: `${formatBytes(summary.memory_used_total)} / ${formatBytes(summary.memory_total_total)}`,
      },
      {
        key: "network",
        label: "网络吞吐",
        icon: "arrow-left-right",
        value: formatThroughput(summary.rx_rate + summary.tx_rate),
        unit: "RX + TX",
        percent: Math.min(100, (summary.rx_rate + summary.tx_rate) / 1e9 * 4),
        tone: "cyan",
        detail: `RX ${formatThroughput(summary.rx_rate)} · TX ${formatThroughput(summary.tx_rate)}`,
      },
    ];
  }

  function alertBannerHtml(alerts) {
    if (!alerts.length) return "";
    return `
      <div class="alert-banner">
        ${icon("triangle-alert")}
        <div class="alert-banner-copy">
          <strong>${alerts.filter((alert) => alert.severity === "critical").length} 个严重告警，${alerts.filter((alert) => alert.severity === "warning").length} 个警告</strong>
          <span>${escapeHtml(alerts[0].node_name)} · ${escapeHtml(alerts[0].title)}</span>
        </div>
        <button class="button ghost compact" data-view="alerts"><span>查看全部</span>${icon("arrow-right")}</button>
      </div>
    `;
  }

  function renderOverview() {
    if (!state.dashboard) return;
    const { summary, groups, alerts, thresholds } = state.dashboard;
    const topCpuRanks = cpuTopRanks(groups.flatMap((group) => group.nodes || []));
    const summaryCards = summaryCardDefinitions(summary, thresholds);
    const actions = `
      <button class="button secondary compact" data-action="scan-network">${icon("radar")}<span>扫描网络</span></button>
      <button class="button secondary compact" data-action="open-bulk-add">${icon("list-plus")}<span>批量纳管</span></button>
    `;
    const alertBanner = alertBannerHtml(alerts);
    const groupsHtml = groups.length
      ? groups
          .map((group) => {
            const online = group.nodes.filter((node) => node.status === "online").length;
            const health = group.nodes.length ? (online / group.nodes.length) * 100 : 0;
            return `
              <section class="group-section" data-group-name="${escapeHtml(group.name)}">
                <div class="group-header">
                  <div class="group-title">
                    <span>${escapeHtml(group.name)}</span>
                    <span class="group-count" data-group-count>${online}/${group.nodes.length} ONLINE</span>
                  </div>
                  <div class="group-health"><i data-group-health-bar style="--health:${health}%"></i><span data-group-health-text>${health.toFixed(0)}%</span></div>
                </div>
                <div class="node-grid">${group.nodes.map((node) => renderNodeCard(node, topCpuRanks.get(Number(node.id)))).join("")}</div>
              </section>
            `;
          })
          .join("")
      : emptyState("server-off", "还没有纳管节点", "扫描局域网 SSH 资产，或直接批量导入节点地址。", "开始扫描", "scan-network");
    $("#content").innerHTML = `
      ${pageHead("实时节点状态", `采样间隔 ${state.dashboard.poll_interval} 秒`, actions, "overview")}
      <div class="overview-summary">
        ${summaryCards.map((card) => summaryCard(card.key, card.label, card.icon, card.value, card.unit, card.percent, card.tone, card.detail)).join("")}
      </div>
      <div id="overviewAlertSlot">${alertBanner}</div>
      ${groupsHtml}
    `;
    requestAnimationFrame(drawAllMiniCharts);
  }

  function patchSummaryCards(summary, thresholds) {
    for (const definition of summaryCardDefinitions(summary, thresholds)) {
      const card = $(`[data-summary-key="${definition.key}"]`);
      if (!card) continue;
      const value = $("[data-summary-value]", card);
      const unit = $("[data-summary-unit]", card);
      const meter = $("[data-summary-meter]", card);
      const detail = $("[data-summary-detail]", card);
      if (value) value.textContent = definition.value;
      if (unit) unit.textContent = definition.unit;
      if (meter) {
        meter.className = definition.tone && definition.tone !== "primary" ? definition.tone : "";
        meter.style.width = `${Math.max(0, Math.min(100, Number(definition.percent) || 0))}%`;
      }
      if (detail) {
        detail.textContent = definition.detail || "";
        detail.hidden = !definition.detail;
      }
    }
  }

  function cpuTopRanks(nodes) {
    const ranked = nodes
      .filter((node) => node.status === "online")
      .map((node) => ({
        id: Number(node.id),
        cpu: Number(node.latest_metrics?.resources?.cpu_percent),
      }))
      .filter((node) => Number.isFinite(node.cpu))
      .sort((left, right) => right.cpu - left.cpu || left.id - right.id)
      .slice(0, 3);
    return new Map(ranked.map((node, index) => [node.id, index + 1]));
  }

  function patchNodeCard(card, node, cpuRank = 0) {
    const { resources, gpus, gpu } = nodeMetrics(node);
    const status = node.status || "unknown";
    const tags = Array.isArray(node.tags) ? node.tags : [];
    const latency = node.last_latency_ms;
    const temperature = Number(resources.temperature_max || 0);
    card.classList.toggle("is-offline", status === "offline");
    card.classList.toggle("is-paused", status === "paused");
    card.classList.remove("cpu-top-1", "cpu-top-2", "cpu-top-3");
    if (cpuRank) card.classList.add(`cpu-top-${cpuRank}`);
    card.dataset.cpuRank = cpuRank ? String(cpuRank) : "";
    card.setAttribute(
      "aria-label",
      `查看 ${node.name} 历史曲线${cpuRank ? `，CPU 负载 Top ${cpuRank}` : ""}`,
    );
    const statusDot = $("[data-node-status]", card);
    if (statusDot) statusDot.className = `node-status ${status}`;
    const name = $("[data-node-name]", card);
    const host = $("[data-node-host]", card);
    const stateText = $("[data-node-state]", card);
    if (name) name.textContent = node.name;
    if (host) host.textContent = node.host;
    if (stateText) {
      stateText.textContent =
        status === "online" && Number.isFinite(Number(latency))
          ? `${Number(latency).toFixed(0)} ms`
          : statusLabel(status);
    }
    const tagsElement = $("[data-node-tags]", card);
    if (tagsElement) {
      tagsElement.innerHTML = `
        <span class="tag accent">${escapeHtml(gpus[0]?.name || gpuFallbackName())}</span>
        <span class="tag">${escapeHtml(node.group_name || defaultGroupName())}</span>
        ${temperature ? `<span class="tag ${temperature >= 80 ? "warn" : ""}">${temperature.toFixed(0)}°C</span>` : ""}
        ${tags.slice(0, 1).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
      `;
    }
    const gpuText = $("[data-node-gpu]", card);
    const gpuBar = $("[data-node-gpu-bar]", card);
    if (gpuText) {
      gpuText.textContent = formatPercent(gpu);
      gpuText.style.color = metricColor(gpu);
    }
    if (gpuBar) {
      gpuBar.className = "";
      gpuBar.style.width = `${Math.min(100, Number(gpu) || 0)}%`;
      gpuBar.style.background = metricColor(gpu);
    }
    const memoryText = $("[data-node-memory]", card);
    const memoryBar = $("[data-node-memory-bar]", card);
    if (memoryText) {
      memoryText.textContent = formatPercent(resources.memory_percent);
      memoryText.style.color = metricColor(resources.memory_percent);
    }
    if (memoryBar) {
      memoryBar.className = "";
      memoryBar.style.width = `${Math.min(100, Number(resources.memory_percent) || 0)}%`;
      memoryBar.style.background = metricColor(resources.memory_percent);
    }
    const cpu = $("[data-node-cpu]", card);
    const memoryUsed = $("[data-node-memory-used]", card);
    const rx = $("[data-node-rx]", card);
    const tx = $("[data-node-tx]", card);
    if (cpu) cpu.textContent = formatPercent(resources.cpu_percent);
    if (memoryUsed) memoryUsed.textContent = formatBytes(resources.memory_used);
    if (rx) rx.textContent = formatThroughput(resources.rx_rate);
    if (tx) tx.textContent = formatThroughput(resources.tx_rate);
  }

  function patchOverview() {
    const dashboard = state.dashboard;
    if (!dashboard) return;
    const cards = $$(".node-card[data-node-id]");
    const nodes = dashboard.nodes || [];
    const topCpuRanks = cpuTopRanks(nodes);
    const currentNodeIds = cards.map((card) => Number(card.dataset.nodeId)).sort((a, b) => a - b);
    const nextNodeIds = nodes.map((node) => Number(node.id)).sort((a, b) => a - b);
    const sameStructure =
      currentNodeIds.length === nextNodeIds.length &&
      currentNodeIds.every((id, index) => id === nextNodeIds[index]) &&
      cards.every((card) => {
        const node = findNode(card.dataset.nodeId);
        return node && card.closest("[data-group-name]")?.dataset.groupName === node.group_name;
      });
    if (!sameStructure) {
      renderOverview();
      return;
    }
    patchSummaryCards(dashboard.summary, dashboard.thresholds);
    const alertSlot = $("#overviewAlertSlot");
    if (alertSlot) {
      const nextAlert = alertBannerHtml(dashboard.alerts || []).trim();
      if (alertSlot.innerHTML.trim() !== nextAlert) {
        alertSlot.innerHTML = nextAlert;
        refreshIcons(alertSlot);
      }
    }
    for (const group of dashboard.groups || []) {
      const section = $(`[data-group-name="${CSS.escape(group.name)}"]`);
      if (!section) continue;
      const online = group.nodes.filter((node) => node.status === "online").length;
      const health = group.nodes.length ? (online / group.nodes.length) * 100 : 0;
      const count = $("[data-group-count]", section);
      const healthText = $("[data-group-health-text]", section);
      const healthBar = $("[data-group-health-bar]", section);
      if (count) count.textContent = `${online}/${group.nodes.length} ONLINE`;
      if (healthText) healthText.textContent = `${health.toFixed(0)}%`;
      if (healthBar) healthBar.style.setProperty("--health", `${health}%`);
    }
    for (const card of cards) {
      const node = findNode(card.dataset.nodeId);
      if (node) patchNodeCard(card, node, topCpuRanks.get(Number(node.id)));
    }
    requestAnimationFrame(drawAllMiniCharts);
  }

  function patchNodeRow(row, node) {
    const { resources, gpu } = nodeMetrics(node);
    const status = $("[data-row-status]", row);
    if (status) status.className = `node-status ${node.status}`;
    const name = $("[data-row-name]", row);
    const state = $("[data-row-state]", row);
    const address = $("[data-row-address]", row);
    const group = $("[data-row-group]", row);
    const tags = $("[data-row-tags]", row);
    const gpuCell = $("[data-row-gpu]", row);
    const cpu = $("[data-row-cpu]", row);
    const memory = $("[data-row-memory]", row);
    const temperature = $("[data-row-temp]", row);
    const seen = $("[data-row-seen]", row);
    if (name) name.textContent = node.name;
    if (state) state.textContent = statusLabel(node.status);
    if (address) address.textContent = sshAddress(node.ssh_user, node.host, node.ssh_port);
    if (group) group.textContent = node.group_name || defaultGroupName();
    if (tags) {
      const nodeTags = (node.tags || []).slice(0, 3);
      tags.innerHTML = nodeTags.length
        ? nodeTags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")
        : '<span class="last-update">—</span>';
    }
    if (gpuCell) gpuCell.textContent = formatPercent(gpu);
    if (cpu) cpu.textContent = formatPercent(resources.cpu_percent);
    if (memory) memory.textContent = formatPercent(resources.memory_percent);
    if (temperature) {
      temperature.textContent = resources.temperature_max
        ? `${formatNumber(resources.temperature_max)}°C`
        : "—";
    }
    if (seen) seen.textContent = timeAgo(node.last_seen);
  }

  function patchNodes() {
    const filtered = filteredNodes();
    const rows = $$("tr[data-node-row]");
    const currentIds = rows.map((row) => Number(row.dataset.nodeRow));
    const nextIds = filtered.map((node) => Number(node.id));
    if (
      currentIds.length !== nextIds.length ||
      currentIds.some((id, index) => id !== nextIds[index])
    ) {
      renderNodes();
      return;
    }
    for (const row of rows) {
      const node = findNode(row.dataset.nodeRow);
      if (node) patchNodeRow(row, node);
    }
  }

  function patchAlerts() {
    const dashboard = state.dashboard;
    if (!dashboard) return;
    const alerts = dashboard.alerts || [];
    const critical = alerts.filter((alert) => alert.severity === "critical").length;
    const warning = alerts.filter((alert) => alert.severity === "warning").length;
    const healthy = Math.max(0, dashboard.summary.total - new Set(alerts.map((alert) => alert.node_id)).size);
    const criticalNode = $('[data-alert-stat="critical"]');
    const warningNode = $('[data-alert-stat="warning"]');
    const healthyNode = $('[data-alert-stat="healthy"]');
    if (criticalNode) criticalNode.textContent = String(critical);
    if (warningNode) warningNode.textContent = String(warning);
    if (healthyNode) healthyNode.textContent = String(healthy);
    const subtitle = $(".page-head-copy p");
    if (subtitle) subtitle.textContent = `${alerts.length} 个活动告警`;
    const list = $("#alertList");
    if (list) {
      list.innerHTML = alerts.length
        ? alerts.map(renderAlertRow).join("")
        : emptyState("shield-check", "当前没有告警", "所有在线节点均在设定阈值内。");
      refreshIcons(list);
    }
  }

  function renderNodeCard(node, cpuRank = 0) {
    const { resources, gpus, gpu } = nodeMetrics(node);
    const status = node.status || "unknown";
    const tags = Array.isArray(node.tags) ? node.tags : [];
    const gpuLabel = gpus[0]?.name || gpuFallbackName();
    const temperature = Number(resources.temperature_max || 0);
    const latency = node.last_latency_ms;
    return `
      <article class="node-card ${status === "offline" ? "is-offline" : ""} ${status === "paused" ? "is-paused" : ""} ${cpuRank ? `cpu-top-${cpuRank}` : ""}" data-node-id="${node.id}" data-cpu-rank="${cpuRank || ""}" role="button" tabindex="0" aria-label="查看 ${escapeHtml(node.name)} 历史曲线${cpuRank ? `，CPU 负载 Top ${cpuRank}` : ""}">
        <div class="node-card-head">
          <span class="node-status ${status}" data-node-status></span>
          <div class="node-title">
            <strong data-node-name>${escapeHtml(node.name)}</strong>
            <span data-node-host>${escapeHtml(node.host)}</span>
          </div>
          <div class="node-card-meta">
            <span class="node-state-text" data-node-state>${status === "online" && Number.isFinite(Number(latency)) ? `${Number(latency).toFixed(0)} ms` : statusLabel(status)}</span>
            ${icon("chart-no-axes-combined", "node-open-hint")}
          </div>
        </div>
        <div class="node-tags" data-node-tags>
          <span class="tag accent">${escapeHtml(gpuLabel)}</span>
          <span class="tag">${escapeHtml(node.group_name || defaultGroupName())}</span>
          ${temperature ? `<span class="tag ${temperature >= 80 ? "warn" : ""}">${temperature.toFixed(0)}°C</span>` : ""}
          ${tags.slice(0, 1).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}
        </div>
        <div class="node-primary-metric">
          <div>
            <div class="metric-line"><span title="${escapeHtml(gpus[0]?.load_source === "power estimate" ? "按功耗与 P-State 估算" : "GPU 驱动利用率")}">${escapeHtml(gpuLoadLabel(gpus[0]))}</span><strong data-node-gpu style="color:${metricColor(gpu)}">${formatPercent(gpu)}</strong></div>
            <div class="bar-track"><i data-node-gpu-bar style="width:${Math.min(100, Number(gpu) || 0)}%;background:${metricColor(gpu)}"></i></div>
          </div>
          <div>
            <div class="metric-line"><span>UMA</span><strong data-node-memory style="color:${metricColor(resources.memory_percent)}">${formatPercent(resources.memory_percent)}</strong></div>
            <div class="bar-track"><i data-node-memory-bar style="width:${Math.min(100, Number(resources.memory_percent) || 0)}%;background:${metricColor(resources.memory_percent)}"></i></div>
          </div>
        </div>
        <div class="node-secondary">
          <div class="mini-metric"><span>CPU</span><strong data-node-cpu>${formatPercent(resources.cpu_percent)}</strong></div>
          <div class="mini-metric"><span>内存</span><strong data-node-memory-used>${formatBytes(resources.memory_used)}</strong></div>
        </div>
        <div class="node-chart-head"><span>GPU 负载趋势</span><span>近 ${escapeHtml(formatHistoryRange(settingValue("ui_history_minutes", 30)))} · 0–100%</span></div>
        <div class="node-chart-wrap">
          <canvas class="node-chart" data-chart-node="${node.id}" aria-label="${escapeHtml(node.name)} 最近 ${escapeHtml(formatHistoryRange(settingValue("ui_history_minutes", 30)))} GPU 负载趋势"></canvas>
          <div class="node-chart-tooltip" data-node-chart-tooltip hidden></div>
        </div>
        <div class="node-net">
          <span>${icon("arrow-down-left")} RX <strong data-node-rx>${formatThroughput(resources.rx_rate)}</strong></span>
          <span>${icon("arrow-up-right")} TX <strong data-node-tx>${formatThroughput(resources.tx_rate)}</strong></span>
        </div>
      </article>
    `;
  }

  function drawAllMiniCharts() {
    $$("canvas[data-chart-node]").forEach((canvas) => {
      const node = findNode(Number(canvas.dataset.chartNode));
      if (!node) return;
      const history =
        state.clusterHistory?.[node.id]?.length > 0
          ? state.clusterHistory[node.id]
          : Array.isArray(node.history)
            ? node.history
            : [];
      const validHistory = history.filter((item) => Number.isFinite(Number(item.gpu)));
      const values = validHistory.map((item) => Number(item.gpu));
      const { gpu } = nodeMetrics(node);
      if (!values.length) values.push(gpu || 0);
      const renderChart = (markerIndex = null) => {
        drawSeriesCanvas(canvas, [{ values, color: "#f1b94c", fill: true }], {
          grid: true,
          max: 100,
          padding: 3,
          markerIndex,
        });
      };
      renderChart();
      const tooltip = canvas.parentElement?.querySelector("[data-node-chart-tooltip]");
      if (!tooltip || !validHistory.length) return;
      canvas.onmousemove = (event) => {
        const bounds = canvas.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
        const index = Math.round(ratio * (validHistory.length - 1));
        const item = validHistory[index];
        renderChart(index);
        tooltip.innerHTML = `
          <strong>${escapeHtml(formatDateTime(item.ts))}</strong>
          <span>GPU 负载 ${formatPercent(item.gpu)}</span>
          <span>CPU ${formatPercent(item.cpu)}</span>
          <span>UMA ${formatPercent(item.memory)}</span>
          <span>温度 ${formatNumber(item.temperature, 0)}°C</span>
        `;
        const left = Math.min(
          bounds.width - 142,
          Math.max(6, event.clientX - bounds.left + 10),
        );
        tooltip.style.left = `${left}px`;
        tooltip.style.top = "4px";
        tooltip.hidden = false;
      };
      canvas.onmouseleave = () => {
        tooltip.hidden = true;
        renderChart();
      };
    });
  }

  let responsiveFrame = 0;
  function scheduleResponsiveRedraw() {
    cancelAnimationFrame(responsiveFrame);
    responsiveFrame = requestAnimationFrame(() => {
      drawAllMiniCharts();
      drawModelThroughputCharts();
      if (state.drawerNodeId) {
        const node = findNode(state.drawerNodeId);
        if (node) {
          patchDrawer(node);
          updateHistoryPanel(node);
        }
      }
    });
  }

  function drawSeriesCanvas(canvas, series, options = {}) {
    if (!canvas || !canvas.isConnected) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const targetWidth = Math.round(rect.width * dpr);
    const targetHeight = Math.round(rect.height * dpr);
    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
      canvas.width = targetWidth;
      canvas.height = targetHeight;
    }
    const context = canvas.getContext("2d");
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    const width = rect.width;
    const height = rect.height;
    const padding = options.padding ?? 8;
    const max = options.max || Math.max(100, ...series.flatMap((item) => item.values.filter(Number.isFinite)));
    context.clearRect(0, 0, width, height);
    if (options.grid) {
      context.strokeStyle = "rgba(255,255,255,0.045)";
      context.lineWidth = 1;
      for (let index = 1; index <= 2; index += 1) {
        const y = padding + ((height - padding * 2) / 3) * index;
        context.beginPath();
        context.moveTo(0, Math.round(y) + 0.5);
        context.lineTo(width, Math.round(y) + 0.5);
        context.stroke();
      }
    }
    for (const item of series) {
      const values = item.values.map(Number).filter(Number.isFinite);
      if (values.length < 1) continue;
      const points = values.map((value, index) => {
        const x = values.length === 1 ? width : (index / (values.length - 1)) * width;
        const normalized = Math.max(0, Math.min(max, value)) / max;
        const y = height - padding - normalized * (height - padding * 2);
        return [x, y];
      });
      if (item.fill) {
        const gradient = context.createLinearGradient(0, 0, 0, height);
        gradient.addColorStop(0, `${item.color}28`);
        gradient.addColorStop(1, `${item.color}00`);
        context.beginPath();
        context.moveTo(points[0][0], height);
        points.forEach(([x, y], index) => {
          if (index === 0) context.lineTo(x, y);
          else {
            const previous = points[index - 1];
            const mid = (previous[0] + x) / 2;
            context.bezierCurveTo(mid, previous[1], mid, y, x, y);
          }
        });
        context.lineTo(points.at(-1)[0], height);
        context.closePath();
        context.fillStyle = gradient;
        context.fill();
      }
      context.beginPath();
      points.forEach(([x, y], index) => {
        if (index === 0) context.moveTo(x, y);
        else {
          const previous = points[index - 1];
          const mid = (previous[0] + x) / 2;
          context.bezierCurveTo(mid, previous[1], mid, y, x, y);
        }
      });
      context.strokeStyle = item.color;
      context.lineWidth = item.width || 1.5;
      context.stroke();
    }
    if (Number.isInteger(options.markerIndex) && series[0]?.values.length) {
      const values = series[0].values.map(Number).filter(Number.isFinite);
      const index = Math.max(0, Math.min(values.length - 1, options.markerIndex));
      const x = values.length === 1 ? width / 2 : (index / (values.length - 1)) * width;
      const normalized = Math.max(0, Math.min(max, values[index])) / max;
      const y = height - padding - normalized * (height - padding * 2);
      context.save();
      context.setLineDash([3, 3]);
      context.strokeStyle = "rgba(255,255,255,0.28)";
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(x, padding);
      context.lineTo(x, height - padding);
      context.stroke();
      context.restore();
      context.beginPath();
      context.arc(x, y, 2.7, 0, Math.PI * 2);
      context.fillStyle = series[0].color;
      context.fill();
    }
  }

  function drawHistoryChart(canvas, history) {
    if (!canvas || !canvas.isConnected) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const targetWidth = Math.round(rect.width * dpr);
    const targetHeight = Math.round(rect.height * dpr);
    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
      canvas.width = targetWidth;
      canvas.height = targetHeight;
    }
    const context = canvas.getContext("2d");
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    const width = rect.width;
    const height = rect.height;
    const margin = { left: 38, right: 12, top: 12, bottom: 25 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    context.clearRect(0, 0, width, height);
    context.font = '9px Consolas, monospace';
    context.textBaseline = "middle";
    context.textAlign = "right";
    for (let value = 0; value <= 100; value += 25) {
      const y = margin.top + plotHeight - (value / 100) * plotHeight;
      context.strokeStyle = "rgba(255,255,255,0.055)";
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(margin.left, Math.round(y) + 0.5);
      context.lineTo(width - margin.right, Math.round(y) + 0.5);
      context.stroke();
      context.fillStyle = "#5f6b64";
      context.fillText(`${value}%`, margin.left - 7, y);
    }
    if (!history.length) return;
    const timestamps = history.map((item) => Number(item.ts)).filter(Number.isFinite);
    const minTime = Math.min(...timestamps);
    const maxTime = Math.max(...timestamps);
    const timeSpan = Math.max(1, maxTime - minTime);
    context.textAlign = "center";
    context.textBaseline = "top";
    const tickCount = width < 420 ? 3 : 4;
    for (let index = 0; index < tickCount; index += 1) {
      const ratio = tickCount === 1 ? 0 : index / (tickCount - 1);
      const x = margin.left + ratio * plotWidth;
      const timestamp = minTime + ratio * timeSpan;
      const label = new Intl.DateTimeFormat("zh-CN", {
        month: timeSpan > 3600 ? "2-digit" : undefined,
        day: timeSpan > 3600 ? "2-digit" : undefined,
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(timestamp * 1000));
      context.fillStyle = "#5f6b64";
      context.fillText(label, x, height - margin.bottom + 8);
    }
    const point = (item, key) => {
      const value = Number(item[key]);
      const x = margin.left + ((Number(item.ts) - minTime) / timeSpan) * plotWidth;
      const y = margin.top + plotHeight - (Math.max(0, Math.min(100, value)) / 100) * plotHeight;
      return [x, y];
    };
    const series = [
      { key: "cpu", color: "#45d17a" },
      { key: "gpu", color: "#f1b94c" },
      { key: "memory", color: "#68a2ff" },
    ];
    for (const item of series) {
      const points = history
        .filter((row) => Number.isFinite(Number(row[item.key])))
        .map((row) => point(row, item.key));
      if (!points.length) continue;
      context.beginPath();
      points.forEach(([x, y], index) => {
        if (index === 0) context.moveTo(x, y);
        else {
          const previous = points[index - 1];
          const mid = (previous[0] + x) / 2;
          context.bezierCurveTo(mid, previous[1], mid, y, x, y);
        }
      });
      context.strokeStyle = item.color;
      context.lineWidth = 1.8;
      context.stroke();
    }
    const tooltip = $("#chartTooltip");
    const wrap = canvas.parentElement;
    if (!tooltip || !wrap) return;
    canvas.onmousemove = (event) => {
      const bounds = canvas.getBoundingClientRect();
      const x = Math.max(margin.left, Math.min(bounds.width - margin.right, event.clientX - bounds.left));
      const ratio = (x - margin.left) / plotWidth;
      const targetTime = minTime + ratio * timeSpan;
      let nearest = history[0];
      for (const item of history) {
        if (Math.abs(Number(item.ts) - targetTime) < Math.abs(Number(nearest.ts) - targetTime)) {
          nearest = item;
        }
      }
      const tooltipX = Math.min(bounds.width - 145, Math.max(8, x + 12));
      tooltip.style.left = `${tooltipX}px`;
      tooltip.style.top = `${Math.max(8, event.clientY - bounds.top - 50)}px`;
      tooltip.innerHTML = `
        <strong>${formatDateTime(nearest.ts)}</strong>
        <span>CPU ${formatPercent(nearest.cpu)}</span>
        <span>GPU ${formatPercent(nearest.gpu)}</span>
        <span>内存 ${formatPercent(nearest.memory)}</span>
        <span>温度 ${formatNumber(nearest.temperature, 0)}°C</span>
      `;
      tooltip.hidden = false;
    };
    canvas.onmouseleave = () => {
      tooltip.hidden = true;
    };
  }

  function renderNodes() {
    const nodes = state.dashboard?.nodes || [];
    const groups = Array.from(new Set(nodes.map((node) => node.group_name || defaultGroupName())));
    const filtered = nodes.filter((node) => {
      const haystack = [node.name, node.host, node.group_name, ...(node.tags || [])]
        .join(" ")
        .toLowerCase();
      return (
        (!state.filters.search || haystack.includes(state.filters.search.toLowerCase())) &&
        (!state.filters.group || node.group_name === state.filters.group) &&
        (!state.filters.status || node.status === state.filters.status)
      );
    });
    const selectedCount = state.selectedNodes.size;
    const bulk = selectedCount
      ? `
        <div class="bulk-toolbar">
          <strong>${selectedCount} 个已选择</strong>
          <button class="button ghost compact" data-bulk-action="group">${icon("folder-tree")}<span>设置分组</span></button>
          <button class="button ghost compact" data-bulk-action="tags">${icon("tags")}<span>设置标签</span></button>
          <button class="button ghost compact" data-bulk-action="disable">${icon("pause")}<span>暂停监控</span></button>
          <button class="button ghost compact" data-bulk-action="enable">${icon("play")}<span>恢复监控</span></button>
          <button class="button ghost compact" data-bulk-action="delete">${icon("trash-2")}<span>删除</span></button>
          <span class="toolbar-spacer"></span>
          <button class="button ghost compact" data-action="clear-selection">取消选择</button>
        </div>
      `
      : "";
    const nodeRows = filtered.map(renderNodeRow).join("");
    const table = filtered.length
      ? `
        <div class="table-wrap">
          <table class="data-table">
            <thead>
              <tr>
                <th><input class="checkbox" id="selectAllNodes" type="checkbox" ${filtered.length && filtered.every((node) => state.selectedNodes.has(node.id)) ? "checked" : ""} aria-label="全选"></th>
                <th>节点</th><th>地址</th><th>分组</th><th>标签</th><th>GPU 负载</th><th>CPU</th><th>内存</th><th>GPU 温度</th><th>最近采集</th><th></th>
              </tr>
            </thead>
            <tbody>${nodeRows}</tbody>
          </table>
        </div>
      `
      : emptyState("search-x", "没有匹配的节点", "调整筛选条件后重试。");
    const actions = `
      <a class="button secondary compact" href="/api/export/nodes">${icon("download")}<span>导出 CSV</span></a>
      <button class="button secondary compact" data-action="scan-network">${icon("radar")}<span>扫描</span></button>
      <button class="primary-button compact" data-action="open-bulk-add">${icon("plus")}<span>添加节点</span></button>
    `;
    $("#content").innerHTML = `
      ${pageHead("资产与批量操作", `${nodes.length} 个节点 · ${nodes.filter((node) => node.status === "online").length} 个在线`, actions, "nodes")}
      <div class="toolbar">
        <div class="search-box">${icon("search")}<input id="nodeSearch" value="${escapeHtml(state.filters.search)}" placeholder="搜索名称、IP、标签"></div>
        <select class="select" id="groupFilter">
          <option value="">全部分组</option>
          ${groups.map((group) => `<option value="${escapeHtml(group)}" ${state.filters.group === group ? "selected" : ""}>${escapeHtml(group)}</option>`).join("")}
        </select>
        <select class="select" id="statusFilter">
          <option value="">全部状态</option>
          ${["online", "offline", "paused", "unknown"].map((status) => `<option value="${status}" ${state.filters.status === status ? "selected" : ""}>${statusLabel(status)}</option>`).join("")}
        </select>
        <span class="toolbar-spacer"></span>
        <span class="last-update">${filtered.length} 条结果</span>
      </div>
      ${bulk}
      ${table}
    `;
  }

  function renderNodeRow(node) {
    const { resources, gpu } = nodeMetrics(node);
    const selected = state.selectedNodes.has(node.id);
    const tags = (node.tags || []).slice(0, 3);
    return `
      <tr data-node-row="${node.id}">
        <td><input class="checkbox select-node" type="checkbox" data-select-node="${node.id}" ${selected ? "checked" : ""} aria-label="选择 ${escapeHtml(node.name)}"></td>
        <td>
          <div class="table-node">
              <span class="node-status ${node.status}" data-row-status></span>
              <div><strong data-row-name>${escapeHtml(node.name)}</strong><span data-row-state>${statusLabel(node.status)}</span></div>
          </div>
        </td>
        <td class="host-mono" data-row-address>${escapeHtml(sshAddress(node.ssh_user, node.host, node.ssh_port))}</td>
        <td data-row-group>${escapeHtml(node.group_name || defaultGroupName())}</td>
        <td><div class="tags-cell" data-row-tags>${tags.length ? tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("") : '<span class="last-update">—</span>'}</div></td>
        <td class="metric-mono" data-row-gpu>${formatPercent(gpu)}</td>
        <td class="metric-mono" data-row-cpu>${formatPercent(resources.cpu_percent)}</td>
        <td class="metric-mono" data-row-memory>${formatPercent(resources.memory_percent)}</td>
        <td class="metric-mono" data-row-temp>${resources.temperature_max ? `${formatNumber(resources.temperature_max)}°C` : "—"}</td>
        <td class="last-update" data-row-seen>${timeAgo(node.last_seen)}</td>
        <td>
          <div class="table-actions">
            <button class="icon-button subtle" data-action="refresh-node" data-id="${node.id}" title="刷新" aria-label="刷新">${icon("refresh-cw")}</button>
            <button class="icon-button subtle" data-action="edit-node" data-id="${node.id}" title="编辑" aria-label="编辑">${icon("pencil")}</button>
            <button class="icon-button subtle danger" data-action="delete-node" data-id="${node.id}" title="删除" aria-label="删除">${icon("trash-2")}</button>
          </div>
        </td>
      </tr>
    `;
  }

  async function loadDiscovery(silent = false) {
    if (state.discoveryBusy) return;
    state.discoveryBusy = true;
    try {
      state.discovery = await api("/api/discovery");
      if (!silent || state.view === "discovery") renderCurrentView();
    } catch (error) {
      if (!silent) showToast(error.message, "error");
    } finally {
      state.discoveryBusy = false;
    }
  }

  function discoveryItemStatus(item) {
    const info = item.info || {};
    if (item.managed) {
      return { label: "已纳管", className: "accent", detail: "" };
    }
    if (info.authenticated === true) {
      return { label: "已认证待纳管", className: "accent", detail: "" };
    }
    if (info.ssh_error) {
      return {
        label: "SSH 认证失败",
        className: "warn",
        detail: String(info.ssh_error),
      };
    }
    return { label: "已发现未认证", className: "", detail: "" };
  }

  function renderDiscovery() {
    const discovery = state.discovery || { discovered: [], scan: {}, interfaces: [] };
    const scan = discovery.scan || {};
    const items = discovery.discovered || [];
    const interfaces = discovery.interfaces || [];
    if (state.scanInterface && !interfaces.some((item) => item.name === state.scanInterface)) {
      state.scanInterface = "";
    }
    const progress = scan.total ? (scan.scanned / scan.total) * 100 : 0;
    const scanLabel = scan.scanning
      ? "正在扫描局域网"
      : scan.error
        ? "扫描范围不可用"
        : scan.finished_at
          ? "扫描完成"
          : "等待扫描";
    const interfaceOptions = [
      `<option value="">${interfaces.length ? "自动选择全部可用网卡" : "未检测到 IPv4 网卡"}</option>`,
      ...interfaces.map((item) => `
        <option value="${escapeHtml(item.name)}" ${state.scanInterface === item.name ? "selected" : ""}>
          ${escapeHtml(item.name)} · ${escapeHtml(item.address)} · ${escapeHtml(item.cidr)}${item.physical ? "" : " · 虚拟"}
        </option>
      `),
    ].join("");
    const scanInterfaces = scan.interface
      ? `网卡 ${scan.interface}`
      : (scan.interfaces || []).length
        ? `网卡 ${(scan.interfaces || []).join(" · ")}`
        : "自动识别网卡";
    const selectedCount = state.selectedDiscovery.size;
    const discoveryRows = items
      .map((item) => {
            const info = item.info || {};
            const selected = state.selectedDiscovery.has(item.host);
            const canImport = info.authenticated === true;
            const status = discoveryItemStatus(item);
            return `
              <tr>
                <td><input class="checkbox discovery-select" type="checkbox" data-discovery-host="${escapeHtml(item.host)}" ${selected ? "checked" : ""} ${!canImport || item.managed ? "disabled" : ""} aria-label="选择 ${escapeHtml(item.host)}"></td>
                <td class="host-mono">${escapeHtml(item.host)}</td>
                <td><strong>${escapeHtml(item.name || info.hostname || item.host)}</strong></td>
                <td class="metric-mono">${escapeHtml(item.machine_id || "—")}</td>
                <td class="metric-mono">${escapeHtml(sshAddress(item.ssh_user, item.host, item.ssh_port))}</td>
                <td>
                  <span class="tag ${escapeHtml(status.className)}">${escapeHtml(status.label)}</span>
                  ${status.detail ? `<span class="discovery-status-detail" title="${escapeHtml(status.detail)}">${escapeHtml(status.detail)}</span>` : ""}
                </td>
                <td class="last-update">${timeAgo(item.last_seen)}</td>
              </tr>
            `;
          })
      .join("");
    const discoveryTable = items.length
      ? `
        <div class="table-wrap">
          <table class="data-table" style="min-width:760px">
            <thead><tr><th></th><th>地址</th><th>节点名</th><th>机器指纹</th><th>SSH</th><th>状态</th><th>最后发现</th></tr></thead>
            <tbody>${discoveryRows}</tbody>
          </table>
        </div>
      `
      : emptyState("radar", "还没有发现节点", "选择本机网卡后扫描对应 IPv4 网段。", "立即扫描", "scan-network-inline");
    $("#content").innerHTML = `
      ${pageHead("自动发现与批量纳管", `${items.length} 个发现结果`, `<button class="button secondary compact" data-action="refresh-discovery">${icon("refresh-cw")}<span>刷新</span></button>`, "discovery")}
      <div class="scan-panel">
        <div class="scan-main">
          <div class="scan-title">${icon(scan.scanning ? "loader-circle" : "radar")}<span>${scanLabel}</span></div>
          ${scan.error ? `<div class="scan-error">${escapeHtml(scan.error)}</div>` : ""}
          <div class="scan-progress"><i style="width:${progress}%"></i></div>
          <div class="scan-meta">
            <span>${scan.scanned || 0} / ${scan.total || 0} HOSTS</span>
            <span>${scan.found || 0} FOUND</span>
            <span>${scan.authenticated || 0} AUTH</span>
            ${scan.probe_failures ? `<span>${scan.probe_failures} AUTH FAILED</span>` : ""}
            <span>${escapeHtml(scanInterfaces)}</span>
            <span>${escapeHtml((scan.subnets || []).join(" · ") || "等待选择范围")}</span>
          </div>
        </div>
        <form class="scan-form" id="scanForm">
          <div class="scan-actions">
            <select class="select scan-interface" id="scanInterface" name="interface" aria-label="扫描网卡">${interfaceOptions}</select>
            <input class="text-input" id="scanCidrs" name="scanCidrs" placeholder="可选 CIDR 覆盖">
            <button class="primary-button compact" type="submit" ${scan.scanning ? "disabled" : ""}>${icon("scan-line")}<span>${scan.scanning ? "扫描中" : "开始扫描"}</span></button>
          </div>
          <div class="scan-credentials form-grid">
            ${sshCredentialFields({
              passwordHint: "密码只用于本次扫描和成功后的加密保存；留空时优先复用已保存凭据。",
            })}
          </div>
        </form>
      </div>
      <div class="discovery-note">${icon("info")}<span>扫描使用所选网卡及其 IPv4 网段。端口开放即可发现主机，完成 SSH 密码或私钥认证后才能纳管并采集。</span></div>
      <div class="toolbar">
        <strong style="font-size:11px;color:var(--text-soft)">发现结果</strong>
        <span class="last-update">${selectedCount} 个已选择</span>
        <span class="toolbar-spacer"></span>
        <button class="primary-button compact" data-action="import-selected" ${selectedCount ? "" : "disabled"}>${icon("plus")}<span>纳管选中节点</span></button>
      </div>
      ${discoveryTable}
    `;
  }

  function renderAlerts() {
    const alerts = state.dashboard?.alerts || [];
    const critical = alerts.filter((alert) => alert.severity === "critical").length;
    const warning = alerts.filter((alert) => alert.severity === "warning").length;
    const list = alerts.length
      ? alerts.map(renderAlertRow).join("")
      : emptyState("shield-check", "当前没有告警", "所有在线节点均在设定阈值内。");
    $("#content").innerHTML = `
      ${pageHead("集群告警", `${alerts.length} 个活动告警`, "", "alerts")}
      <div class="alert-summary">
        <div class="alert-stat"><div class="alert-stat-icon critical">${icon("octagon-alert")}</div><div><strong data-alert-stat="critical">${critical}</strong><span>严重告警</span></div></div>
        <div class="alert-stat"><div class="alert-stat-icon warning">${icon("triangle-alert")}</div><div><strong data-alert-stat="warning">${warning}</strong><span>性能警告</span></div></div>
        <div class="alert-stat"><div class="alert-stat-icon">${icon("check-circle-2")}</div><div><strong data-alert-stat="healthy">${Math.max(0, (state.dashboard?.summary.total || 0) - new Set(alerts.map((alert) => alert.node_id)).size)}</strong><span>健康节点</span></div></div>
      </div>
      ${renderAlertRulesPanel()}
      <div class="alert-list" id="alertList">${list}</div>
    `;
  }

  function renderAlertRulesPanel() {
    const rules = state.alertRules?.rules || [];
    if (!rules.length) return "";
    const rows = rules
      .map(
        (rule) => `
        <div class="alert-rule-row" data-rule-key="${escapeHtml(rule.key)}">
          <span class="severity ${rule.severity}">${rule.severity === "critical" ? "严重" : "警告"}</span>
          <div class="alert-rule-copy">
            <strong>${escapeHtml(rule.display)}</strong>
            <small>${escapeHtml(rule.description)}</small>
          </div>
          <div class="alert-rule-input">
            <input type="number" class="text-input" data-rule-input="${escapeHtml(rule.key)}" value="${escapeHtml(rule.value)}" min="${escapeHtml(rule.min)}" max="${escapeHtml(rule.max)}" step="1" aria-label="${escapeHtml(rule.label)}">
            <em>${escapeHtml(rule.unit)}</em>
          </div>
          <span class="alert-rule-triggered ${rule.triggered ? "hit" : ""}" data-rule-triggered="${escapeHtml(rule.key)}">${rule.triggered ? `${rule.triggered} 个节点触发` : "未触发"}</span>
        </div>`,
      )
      .join("");
    return `
      <section class="alert-rules-panel">
        <div class="alert-rules-head">
          <h3>告警规则 ${helpButton("alert-rules")}</h3>
          <div class="alert-rules-actions">
            <button class="button ghost compact" data-action="alert-rules-reset">${icon("rotate-ccw")}<span>恢复默认</span></button>
            <button class="button secondary compact" data-action="alert-rules-save">${icon("save")}<span>保存规则</span></button>
          </div>
        </div>
        <div class="alert-rule-list">${rows}</div>
      </section>
    `;
  }

  async function loadAlertRules() {
    try {
      const payload = await api("/api/alert-rules");
      state.alertRules = payload;
      if (state.view === "alerts") renderAlerts();
    } catch (error) {
      if (error.status !== 401) showToast(error.message, "error");
    }
  }

  function collectAlertRules() {
    const rules = {};
    for (const input of $$("[data-rule-input]")) {
      const value = Number(input.value);
      if (!Number.isFinite(value)) return null;
      rules[input.dataset.ruleInput] = value;
    }
    return Object.keys(rules).length ? rules : null;
  }

  async function saveAlertRules() {
    const rules = collectAlertRules();
    if (!rules) {
      showToast("阈值必须是有效数字", "error");
      return;
    }
    try {
      state.alertRules = await api("/api/alert-rules", { method: "PATCH", body: { rules } });
      renderAlerts();
      refreshIcons($("#content"));
      showToast("告警规则已保存，下一轮采集生效", "success");
      await loadDashboard(true);
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  function resetAlertRules() {
    const rules = state.alertRules?.rules || [];
    if (!rules.length) return;
    confirmDialog(
      "恢复默认告警规则",
      "将把 CPU、内存、GPU、温度和离线判定恢复为默认阈值。",
      "恢复默认",
      async () => {
        const defaults = {};
        for (const rule of rules) defaults[rule.key] = rule.default;
        try {
          state.alertRules = await api("/api/alert-rules", {
            method: "PATCH",
            body: { rules: defaults },
          });
          renderAlerts();
          refreshIcons($("#content"));
          showToast("已恢复默认告警规则", "success");
          await loadDashboard(true);
        } catch (error) {
          showToast(error.message, "error");
        }
      },
      false,
    );
  }

  function renderAlertRow(alert) {
    const critical = alert.severity === "critical";
    return `
      <div class="alert-row ${critical ? "critical" : ""}">
        <div class="alert-row-icon">${icon(critical ? "octagon-alert" : "triangle-alert")}</div>
        <div class="alert-row-copy">
          <strong>${escapeHtml(alert.node_name)} · ${escapeHtml(alert.title)}</strong>
          <span>${escapeHtml(alert.host)} · ${escapeHtml(alert.message)}</span>
        </div>
        <span class="severity ${alert.severity}">${critical ? "严重" : "警告"}</span>
        <button class="button ghost compact" data-node-id="${alert.node_id}">${icon("arrow-up-right")}<span>查看</span></button>
      </div>
    `;
  }

  async function loadAudit(silent = false) {
    try {
      const payload = await api("/api/audit");
      state.audit = payload.logs || [];
      if (!silent || state.view === "audit") renderCurrentView();
    } catch (error) {
      if (!silent) showToast(error.message, "error");
    }
  }

  function renderAudit() {
    const rows = state.audit.length
      ? state.audit
          .map(
            (log) => `
            <div class="audit-row">
              <span class="audit-meta">${formatDateTime(log.ts)}</span>
              <span class="audit-action">${escapeHtml(log.action)}</span>
              <span class="audit-target">${escapeHtml(log.target || "—")}</span>
              <span class="audit-detail">${escapeHtml(log.detail || "—")}</span>
              <span class="audit-meta">${escapeHtml(log.username || "system")} · ${escapeHtml(log.remote_addr || "local")}</span>
            </div>
          `,
          )
          .join("")
      : emptyState("scroll-text", "还没有操作记录", "登录、纳管和批量操作会记录在这里。");
    $("#content").innerHTML = `
      ${pageHead("管理员操作记录", `${state.audit.length} 条最近记录`, `<button class="button secondary compact" data-action="export-audit">${icon("download")}<span>导出视图</span></button>`, "audit")}
      <div class="audit-list">${rows}</div>
    `;
  }

  function modelStatusLabel(status) {
    return { ready: "运行正常", degraded: "部分异常", offline: "不可用", unavailable: "未知" }[status] || "未知";
  }

  function modelKpiDefinitions(data) {
    const inference = data.inference || {};
    return [
      {
        key: "running",
        label: "当前并发",
        value: inference.running_requests == null ? "—" : formatNumber(inference.running_requests),
        unit: "REQ",
        detail: inference.last_batch_type ? `最近批次 ${inference.last_batch_type}` : "等待推理请求",
        tone: "primary",
      },
      {
        key: "queue",
        label: "请求队列",
        value: inference.queued_requests == null ? "—" : formatNumber(inference.queued_requests),
        unit: "WAIT",
        detail: "等待调度的请求",
        tone: Number(inference.queued_requests || 0) > 0 ? "warning" : "",
      },
      {
        key: "prefill",
        label: "Prefill 吞吐",
        value: inference.prefill_tokens_per_sec == null ? "—" : formatNumber(inference.prefill_tokens_per_sec, 2),
        unit: "tok/s",
        detail: inference.last_prefill_at || "暂无 Prefill 批次",
        tone: "cyan",
      },
      {
        key: "decode",
        label: "Decode 吞吐",
        value: inference.decode_tokens_per_sec == null ? "—" : formatNumber(inference.decode_tokens_per_sec, 2),
        unit: "tok/s",
        detail: inference.last_decode_at || "暂无 Decode 批次",
        tone: "cyan",
      },
      {
        key: "pending",
        label: "待处理 Token",
        value: inference.pending_tokens == null ? "—" : formatNumber(inference.pending_tokens),
        unit: "TOKENS",
        detail: "最近 Prefill 批次",
        tone: "",
      },
      {
        key: "kv",
        label: "KV / Token 占用",
        value: inference.full_token_usage_pct == null ? "—" : formatNumber(inference.full_token_usage_pct, 1),
        unit: "%",
        detail: "最近批次显存 token 使用率",
        tone: "",
      },
      {
        key: "accept",
        label: "投机接受率",
        value: inference.accept_rate_pct == null ? "—" : formatNumber(inference.accept_rate_pct, 2),
        unit: "%",
        detail: "最近 Decode 批次",
        tone: "",
      },
      {
        key: "cached",
        label: "Prefix 缓存命中",
        value:
          inference.prefix_cache_hit_rate_pct == null
            ? "—"
            : formatNumber(inference.prefix_cache_hit_rate_pct, 1),
        unit: "%",
        detail: inference.metrics_source || "批次统计",
        tone: "",
      },
    ];
  }

  function modelThroughputChartDefinitions(data) {
    const inference = data.inference || {};
    const configuredMinutes = Math.max(
      1,
      Number(
        data.throughput_history?.window_minutes ||
          settingValue("model_history_minutes", 30),
      ),
    );
    const configuredBuckets = Math.max(
      2,
      Number(
        data.throughput_history?.bucket_seconds
          ? Math.round(
              (configuredMinutes * 60) /
                Number(data.throughput_history.bucket_seconds),
            )
          : settingValue("model_history_buckets", 30),
      ),
    );
    const bucketSeconds = Math.max(
      1,
      Number(
        data.throughput_history?.bucket_seconds ||
          Math.round((configuredMinutes * 60) / configuredBuckets),
      ),
    );
    const currentBucket = Math.floor(Date.now() / 1000 / bucketSeconds) * bucketSeconds;
    let points = Array.isArray(data.throughput_history?.points)
      ? data.throughput_history.points.slice(-configuredBuckets)
      : [];
    if (!points.length) {
      points = Array.from({ length: configuredBuckets }, (_, index) => ({
        ts: currentBucket - (configuredBuckets - 1 - index) * bucketSeconds,
        prefill_tokens_per_sec: null,
        decode_tokens_per_sec: null,
      }));
      points[points.length - 1].prefill_tokens_per_sec = inference.prefill_tokens_per_sec;
      points[points.length - 1].decode_tokens_per_sec = inference.decode_tokens_per_sec;
    }
    const series = [
      {
        key: "prefill",
        label: "Prefill",
        field: "prefill_tokens_per_sec",
        detail: inference.last_prefill_at || "暂无 Prefill 批次",
        color: "#70e1ed",
      },
      {
        key: "decode",
        label: "Decode",
        field: "decode_tokens_per_sec",
        detail: inference.last_decode_at || "暂无 Decode 批次",
        color: "#67df91",
      },
    ];
    return series.map((item) => {
      const values = points.map((point) => {
        const value = Number(point[item.field]);
        return Number.isFinite(value) ? Math.max(0, value) : null;
      });
      const maximum = Math.max(1, ...values.filter((value) => value != null));
      const latest = values[values.length - 1];
      return {
        ...item,
        value: latest == null ? "—" : formatNumber(latest, 2),
        peak: formatNumber(maximum, 2),
        maximum,
        points: points.map((point, index) => ({
          ts: point.ts,
          value: values[index],
          label: bucketSeconds >= 60 ? formatMinuteClock(point.ts) : formatClock(point.ts),
        })),
      };
    });
  }

  function drawModelThroughputChart(canvas, item, markerIndex = null) {
    if (!canvas || !canvas.isConnected) return;
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const width = rect.width;
    const height = rect.height;
    const targetWidth = Math.round(width * dpr);
    const targetHeight = Math.round(height * dpr);
    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
      canvas.width = targetWidth;
      canvas.height = targetHeight;
    }
    const context = canvas.getContext("2d");
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    const padding = 8;
    const plotWidth = Math.max(1, width - padding * 2);
    const plotHeight = Math.max(1, height - padding * 2);
    const maximum = Number(item.maximum) || 1;
    context.strokeStyle = "rgba(255,255,255,0.055)";
    context.lineWidth = 1;
    for (let index = 0; index <= 3; index += 1) {
      const y = padding + plotHeight * index / 3;
      context.beginPath();
      context.moveTo(padding, Math.round(y) + 0.5);
      context.lineTo(width - padding, Math.round(y) + 0.5);
      context.stroke();
    }
    const xFor = (index) => item.points.length <= 1
      ? width / 2
      : padding + index / (item.points.length - 1) * plotWidth;
    const yFor = (value) => height - padding - Math.min(maximum, Math.max(0, value)) / maximum * plotHeight;
    context.strokeStyle = item.color;
    context.lineWidth = 1.8;
    context.lineJoin = "round";
    context.lineCap = "round";
    let segmentOpen = false;
    const validPoints = [];
    item.points.forEach((point, index) => {
      if (point.value == null) {
        segmentOpen = false;
        return;
      }
      const x = xFor(index);
      const y = yFor(point.value);
      validPoints.push([x, y]);
      if (!segmentOpen) {
        context.beginPath();
        context.moveTo(x, y);
        segmentOpen = true;
      } else {
        context.lineTo(x, y);
      }
      if (index === item.points.length - 1 || item.points[index + 1]?.value == null) {
        context.stroke();
        segmentOpen = false;
      }
    });
    if (validPoints.length === 1) {
      context.beginPath();
      context.arc(validPoints[0][0], validPoints[0][1], 2.4, 0, Math.PI * 2);
      context.fillStyle = item.color;
      context.fill();
    }
    if (Number.isInteger(markerIndex)) {
      const point = item.points[markerIndex];
      if (point?.value != null) {
        const x = xFor(markerIndex);
        const y = yFor(point.value);
        context.save();
        context.setLineDash([3, 3]);
        context.strokeStyle = "rgba(255,255,255,0.28)";
        context.lineWidth = 1;
        context.beginPath();
        context.moveTo(x, padding);
        context.lineTo(x, height - padding);
        context.stroke();
        context.restore();
        context.beginPath();
        context.arc(x, y, 2.8, 0, Math.PI * 2);
        context.fillStyle = item.color;
        context.fill();
      }
    }
  }

  function drawModelThroughputCharts() {
    if (state.view !== "model" || !state.modelOverview) return;
    for (const item of modelThroughputChartDefinitions(state.modelOverview)) {
      const canvas = $(`[data-model-throughput="${item.key}"] [data-throughput-canvas]`);
      const tooltip = $(`[data-model-throughput="${item.key}"] [data-throughput-tooltip]`);
      if (!canvas || !tooltip) continue;
      const redraw = (markerIndex = null) => drawModelThroughputChart(canvas, item, markerIndex);
      redraw();
      canvas.onmousemove = (event) => {
        const bounds = canvas.getBoundingClientRect();
        const ratio = Math.max(
          0,
          Math.min(1, (event.clientX - bounds.left - 8) / Math.max(1, bounds.width - 16)),
        );
        const index = Math.round(ratio * (item.points.length - 1));
        const point = item.points[index];
        redraw(index);
        tooltip.innerHTML = `
          <strong>${escapeHtml(point.label)}</strong>
          <span>${escapeHtml(item.label)} ${point.value == null ? "—" : formatNumber(point.value, 2)} tok/s</span>
        `;
        tooltip.style.left = `${Math.min(bounds.width - 120, Math.max(6, event.clientX - bounds.left + 10))}px`;
        tooltip.hidden = false;
      };
      canvas.onmouseleave = () => {
        tooltip.hidden = true;
        redraw();
      };
    }
  }

  function modelFieldDefinitions(data) {
    const inference = data.inference || {};
    const config = data.config || {};
    const container = data.container || {};
    const cluster = data.cluster || {};
    const kvBytes = Number(config.kv_cache_memory_bytes);
    const gpuRatio = Number(config.gpu_memory_utilization);
    return [
      ["status", modelStatusLabel(data.status)],
      ["engine", data.engine_label || "Unknown"],
      [
        "usage-total",
        inference.total_tokens == null
          ? "—"
          : formatTokenMillions(inference.total_tokens),
      ],
      [
        "usage-input",
        inference.prompt_tokens_total == null
          ? "—"
          : formatTokenMillions(inference.prompt_tokens_total),
      ],
      [
        "usage-output",
        inference.generation_tokens_total == null
          ? "—"
          : formatTokenMillions(inference.generation_tokens_total),
      ],
      [
        "usage-requests",
        inference.request_count_total == null
          ? "—"
          : formatNumber(inference.request_count_total),
      ],
      ["model-name", data.model?.name || "unknown"],
      ["endpoint", data.endpoint || "—"],
      ["model-started", formatIsoDateTime(inference.data_since || container.started_at)],
      ["health", `${data.model?.health_nodes || 0}/${data.model?.total_nodes || 0} 节点健康`],
      ["container-status", `${container.status || "unknown"}${container.health ? ` · ${container.health}` : ""}`],
      ["container-uptime", container.uptime == null ? "—" : formatDuration(container.uptime)],
      ["container-image", container.image || "—"],
      ["tp-ep", `TP ${config.tensor_parallel_size || "—"} / EP ${config.expert_parallel_size || "—"}`],
      ["max-requests", config.max_running_requests || "—"],
      [
        "max-num-seqs",
        config.max_num_seqs || config.max_running_requests || "—",
      ],
      [
        "kv-cache-memory",
        Number.isFinite(kvBytes) && kvBytes > 0
          ? `${formatNumber(kvBytes / 1024 ** 3, 0)} GiB`
          : "—",
      ],
      [
        "gpu-memory-utilization",
        Number.isFinite(gpuRatio) && gpuRatio > 0
          ? formatPercent(gpuRatio * 100, 0)
          : "—",
      ],
      ["block-size", config.block_size || "—"],
      [
        "prefix-cache-retention",
        config.prefix_cache_retention_interval
          ? `${formatNumber(config.prefix_cache_retention_interval)} tokens`
          : "—",
      ],
      [
        "long-prefill-threshold",
        config.long_prefill_token_threshold
          ? `${formatNumber(config.long_prefill_token_threshold)} tokens`
          : "—",
      ],
      ["context-length", config.context_length ? `${formatNumber(Number(config.context_length) / 1000, 0)}K` : "—"],
      ["speculative", config.speculative_algorithm ? `${config.speculative_algorithm} · block ${config.speculative_block_size || "—"}` : "—"],
      ["model-path", config.model_path || "—"],
      ["cluster-gpu", cluster.gpu_load_avg == null ? "—" : `${formatPercent(cluster.gpu_load_avg)}`],
      ["cluster-temp", cluster.temperature_max == null ? "—" : `${formatNumber(cluster.temperature_max)}°C`],
      ["cluster-memory", cluster.memory_avg == null ? "—" : formatPercent(cluster.memory_avg)],
      ["cluster-network", formatThroughput((cluster.network_rx_rate || 0) + (cluster.network_tx_rate || 0))],
      ["ttft", inference.ttft_avg_ms == null ? "—" : `${formatNumber(inference.ttft_avg_ms, 1)} ms`],
      ["e2e", inference.e2e_avg_ms == null ? "—" : `${formatNumber(inference.e2e_avg_ms, 1)} ms`],
      ["inter-token", inference.inter_token_avg_ms == null ? "—" : `${formatNumber(inference.inter_token_avg_ms, 1)} ms`],
      ["success-requests", inference.successful_requests == null ? "—" : formatNumber(inference.successful_requests)],
      ["failed-requests", inference.failed_requests == null ? "—" : formatNumber(inference.failed_requests)],
      ["last-batch", inference.last_batch_type || "—"],
      ["last-prefill", inference.last_prefill_at || "—"],
      ["last-decode", inference.last_decode_at || "—"],
      ["log-errors", formatNumber(inference.log_error_count)],
    ];
  }

  async function loadChatInfo() {
    try {
      state.chatInfo = await api("/api/model/chat/info");
    } catch (error) {
      state.chatInfo = { available: false, error: error.message, models: [], endpoint: "" };
    }
    if (state.view === "chat") renderChat();
  }

  function formatChatMs(value) {
    if (value == null || !Number.isFinite(Number(value))) return "—";
    const ms = Number(value);
    if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`;
    return `${ms.toFixed(0)} ms`;
  }

  function chatMessageHtml(message) {
    const isUser = message.role === "user";
    const m = message.metrics;
    const metricsHtml = m
      ? `<div class="chat-bubble-metrics">输入 ${m.prompt_tokens} tok · 输出 ${m.completion_tokens} tok · TTFT ${formatChatMs(m.ttft_ms)} · 总 ${formatChatMs(m.total_ms)} · ${m.throughput} tok/s</div>`
      : "";
    const reasoningHtml =
      !isUser && message.reasoning
        ? `<details class="chat-reasoning"><summary>思考过程 · ${message.reasoning.length} 字符</summary><div>${escapeHtml(message.reasoning)}</div></details>`
        : "";
    const body = message.content || (message.reasoning ? "（仅输出思考内容，未产生正式回复）" : "(空响应)");
    return `
      <div class="chat-bubble ${isUser ? "user" : "assistant"}">
        <div class="chat-bubble-role">${isUser ? "你" : "模型"}</div>
        ${reasoningHtml}
        <div class="chat-bubble-content">${escapeHtml(body)}</div>
        ${metricsHtml}
      </div>`;
  }

  function renderChatMessages() {
    const box = $("#chatMessages");
    if (!box) return;
    box.innerHTML = state.chat.messages.length
      ? state.chat.messages.map(chatMessageHtml).join("")
      : `<div class="chat-empty">${icon("message-square-dashed")}<span>发送一条消息开始测试</span></div>`;
    box.scrollTop = box.scrollHeight;
  }

  function chatStatsHtml() {
    const last = state.chat.lastStats;
    const totals = state.chat.totals;
    const avg = totals.genMs > 0 ? (totals.completion / (totals.genMs / 1000)).toFixed(1) : "—";
    return `
      <div class="chat-side-title">本轮指标 <small>THIS TURN</small></div>
      <div class="chat-stat-grid">
        <div class="chat-stat"><span>输入 Token</span><strong>${last ? last.prompt_tokens : "—"}</strong></div>
        <div class="chat-stat"><span>输出 Token</span><strong>${last ? last.completion_tokens : "—"}</strong></div>
        <div class="chat-stat"><span>TTFT</span><strong>${last ? formatChatMs(last.ttft_ms) : "—"}</strong></div>
        <div class="chat-stat"><span>总耗时</span><strong>${last ? formatChatMs(last.total_ms) : "—"}</strong></div>
        <div class="chat-stat"><span>吞吐</span><strong>${last ? `${last.throughput} tok/s` : "—"}</strong></div>
        <div class="chat-stat"><span>结束原因</span><strong>${last?.finish_reason || "—"}</strong></div>
      </div>
      <div class="chat-total">
        <span>会话累计 <small>SESSION</small></span>
        <strong>${totals.turns} 轮 · 输入 ${totals.prompt} · 输出 ${totals.completion} tok</strong>
        <small>平均生成 ${avg} tok/s</small>
      </div>`;
  }

  function renderChat() {
    const info = state.chatInfo;
    const models = info?.models || [];
    const options = models.length
      ? models.map((m) => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join("")
      : '<option value="">未发现模型</option>';
    const busy = state.chat.busy;
    $("#content").innerHTML = `
      ${pageHead(
        "模型测试",
        info ? (info.available ? `已连接 ${info.endpoint}` : "模型服务不可用") : "正在检测模型服务",
        `<button class="button ghost compact" data-action="chat-reset">${icon("rotate-ccw")}<span>新会话</span></button>`,
        "chat",
      )}
      ${info && info.available === false ? `<div class="panel chat-offline">${icon("alert-triangle")}<span>${escapeHtml(info.error || "模型服务不可用")}</span></div>` : ""}
      <section class="chat-layout">
        <div class="chat-main">
          <div class="chat-toolbar">
            <label class="chat-field">模型<select id="chatModel" class="select">${options}</select></label>
            <label class="chat-field">最大输出<input id="chatMaxTokens" class="text-input" type="number" value="512" min="1" max="8192"></label>
            <label class="chat-field">温度<input id="chatTemperature" class="text-input" type="number" value="0.7" min="0" max="2" step="0.1"></label>
            <label class="chat-field chat-check"><input id="chatThinking" type="checkbox"> 启用思考</label>
          </div>
          <div class="chat-messages" id="chatMessages"></div>
          <div class="chat-composer">
            <textarea id="chatInput" rows="2" placeholder="输入消息，Enter 发送，Shift+Enter 换行" ${busy ? "disabled" : ""}></textarea>
            <button class="primary-button" data-action="chat-send" ${busy ? "disabled" : ""}>${icon(busy ? "loader-circle" : "send")}<span>${busy ? "生成中" : "发送"}</span></button>
          </div>
        </div>
        <aside class="chat-side ${busy ? "busy" : ""}">${chatStatsHtml()}</aside>
      </section>`;
    renderChatMessages();
    refreshIcons($("#content"));
  }

  async function sendChat() {
    if (state.chat.busy) return;
    const input = $("#chatInput");
    const text = (input?.value || "").trim();
    if (!text) return;
    state.chat.messages.push({ role: "user", content: text });
    state.chat.busy = true;
    const modelValue = $("#chatModel")?.value || "";
    const maxTokens = Number($("#chatMaxTokens")?.value) || 512;
    const temperature = Number($("#chatTemperature")?.value ?? 0.7);
    renderChat();
    try {
      const payload = await api("/api/model/chat", {
        method: "POST",
        body: {
          model: modelValue,
          max_tokens: maxTokens,
          temperature,
          enable_thinking: Boolean($("#chatThinking")?.checked),
          messages: state.chat.messages.map((m) => ({ role: m.role, content: m.content })),
        },
      });
      state.chat.lastStats = { ...(payload.usage || {}), ...(payload.metrics || {}) };
      state.chat.messages.push({
        role: "assistant",
        content: payload.content || "",
        reasoning: payload.reasoning || "",
        metrics: state.chat.lastStats,
      });
      const totals = state.chat.totals;
      totals.turns += 1;
      totals.prompt += Number(payload.usage?.prompt_tokens || 0);
      totals.completion += Number(payload.usage?.completion_tokens || 0);
      totals.genMs += Number(payload.metrics?.generation_ms || 0);
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      state.chat.busy = false;
      if (state.view === "chat") renderChat();
    }
  }

  function resetChat() {
    confirmDialog("开始新会话", "将清空当前对话和统计。", "清空", () => {
      state.chat.messages = [];
      state.chat.lastStats = null;
      state.chat.totals = { turns: 0, prompt: 0, completion: 0, genMs: 0 };
      renderChat();
    });
  }

  function modelServiceStateInfo(service) {
    const phase = String(service.phase || "");
    const label = service.phase_label || "";
    if (phase === "ready") return { key: "ready", label: label || "已就绪", cls: "active" };
    if (phase === "starting") return { key: "starting", label: label || "启动中", cls: "starting" };
    if (phase === "warming") return { key: "warming", label: label || "引擎加载中", cls: "starting" };
    if (phase === "completed") return { key: "completed", label: label || "脚本已完成", cls: "stopping" };
    if (phase === "failed") return { key: "failed", label: label || "启动失败", cls: "failed" };
    if (phase === "stopping") return { key: "stopping", label: label || "停止中", cls: "stopping" };
    return { key: "stopped", label: label || "已停止", cls: "inactive" };
  }

  function modelServiceTaskHtml(task) {
    if (!task) return "";
    const actionLabel = { start: "启动", stop: "停止", switch: "切换" }[task.action] || task.action;
    const statusIcon = task.status === "running" ? icon("loader-circle", "spin") : task.status === "done" ? icon("check") : icon("alert-triangle");
    return `
      <div class="model-service-task ${escapeHtml(task.status)}">
        <span class="model-service-task-icon">${statusIcon}</span>
        <div class="model-service-task-copy">
          <strong>${escapeHtml(actionLabel)} · ${escapeHtml(task.unit || "")}</strong>
          <small>${escapeHtml(task.message || "")}</small>
        </div>
      </div>`;
  }

  function modelServicesPanelHtml() {
    const data = state.modelServices;
    if (!data) {
      return `
        <div class="model-panel-head"><h3>模型服务控制 <small>MODEL SERVICES · HEAD</small></h3><span>读取中</span></div>
        <div class="model-service-loading">${icon("loader-circle", "spin")}<span>正在读取 HEAD 节点上的 model_* 服务</span></div>`;
    }
    const head = data.head?.name || data.head?.host || "HEAD";
    const header = `<div class="model-panel-head"><h3>模型服务控制 <small>MODEL SERVICES · HEAD</small>${helpButton("model-services")}</h3><span>${escapeHtml(head)} · ${(data.services || []).length} 个服务</span></div>`;
    if (data.available === false) {
      return `${header}<div class="model-service-error">${icon("alert-triangle")}<span>${escapeHtml(data.error || "HEAD 节点不可达，无法读取模型服务")}</span></div>`;
    }
    const services = data.services || [];
    const busy = Boolean(data.task && data.task.status === "running");
    if (!services.length) {
      return `${header}<div class="model-service-empty">未发现 model_*.service，请确认 HEAD 节点已配置模型服务单元</div>`;
    }
    const rows = services
      .map((service) => {
        const info = modelServiceStateInfo(service);
        const running = ["active", "activating", "reloading", "deactivating"].includes(String(service.active_state));
        const unit = escapeHtml(service.unit);
        return `
          <div class="model-service-row ${info.cls}">
            <span class="model-service-dot ${info.cls}"></span>
            <div class="model-service-copy">
              <strong>${escapeHtml(service.name || service.unit)}</strong>
              <small>${escapeHtml(service.description || service.unit)}</small>
            </div>
            <div class="model-service-phase ${info.cls}" title="${escapeHtml([service.phase_detail, service.error_hint].filter(Boolean).join(" · "))}">
              <strong>${escapeHtml(info.label)}</strong>
              ${service.phase_detail ? `<small>${escapeHtml(service.phase_detail)}</small>` : ""}
              ${typeof service.progress === "number" ? `<span class="phase-bar"><i style="width:${Math.max(1, Math.min(100, service.progress))}%"></i></span>` : ""}
            </div>
            <div class="model-service-actions">
              <button class="button ghost compact" data-action="model-service-start" data-unit="${unit}" ${busy || running ? "disabled" : ""}>${icon("play")}<span>启动</span></button>
              <button class="button ghost compact danger" data-action="model-service-stop" data-unit="${unit}" ${busy || !running ? "disabled" : ""}>${icon("square")}<span>停止</span></button>
              <button class="button secondary compact" data-action="model-service-switch" data-unit="${unit}" ${busy || running ? "disabled" : ""}>${icon("repeat")}<span>切换到此</span></button>
            </div>
          </div>`;
      })
      .join("");
    return `${header}${modelServiceTaskHtml(data.task)}<div class="model-service-list">${rows}</div>
      <div class="model-service-hint">启动 / 停止 / 切换均在后台执行（8 节点 oneshot，通常数分钟），任务进度会自动刷新。</div>`;
  }

  function renderModelServicesPanel() {
    const section = $("#modelServicePanel");
    if (!section) return;
    section.innerHTML = modelServicesPanelHtml();
    refreshIcons(section);
  }

  async function loadModelServices(silent = false) {
    if (state.modelServicesLoading) return state.modelServices;
    state.modelServicesLoading = true;
    try {
      const data = await api("/api/model/services");
      state.modelServices = data;
      if (data?.task?.status === "running") startModelTaskPolling();
      if (state.view === "model") {
        if (!silent || !$("#modelServicePanel") || !$(".model-status-band")) renderModel();
        else renderModelServicesPanel();
      }
      return data;
    } catch (error) {
      if (error.status !== 401) showToast(error.message, "error");
      return null;
    } finally {
      state.modelServicesLoading = false;
    }
  }

  function startModelTaskPolling() {
    if (state.modelTaskTimer) return;
    state.modelTaskTimer = setInterval(async () => {
      const data = await loadModelServices(true);
      const task = data?.task;
      if (!task || task.status !== "running") {
        clearInterval(state.modelTaskTimer);
        state.modelTaskTimer = null;
        await loadModelServices(true);
        if (task) {
          showToast(
            task.status === "failed" ? `任务失败：${task.message}` : `任务完成：${task.unit}`,
            task.status === "failed" ? "error" : "success",
          );
        }
      }
    }, 4000);
  }

  function askModelServiceAction(action, unit) {
    const info = (state.modelServices?.services || []).find((item) => item.unit === unit);
    const label = { start: "启动", stop: "停止", switch: "切换" }[action] || action;
    const name = info?.name || unit;
    if (action === "switch") {
      confirmDialog(
        "切换模型服务",
        `将停止其他运行中的模型服务并启动 ${name}。8 节点启动通常需要数分钟，期间推理服务不可用。确认切换？`,
        "确认切换",
        () => runModelServiceAction(action, unit),
        true,
      );
    } else {
      confirmDialog(
        `${label}模型服务`,
        `${label} ${name}？${action === "stop" ? "停止后推理服务将不可用。" : ""}`,
        `确认${label}`,
        () => runModelServiceAction(action, unit),
        action === "stop",
      );
    }
  }

  async function runModelServiceAction(action, unit) {
    if (state.modelServiceBusy) return;
    state.modelServiceBusy = true;
    try {
      const result = await api("/api/model/services/action", {
        method: "POST",
        body: { action, unit },
      });
      state.modelServices = { ...(state.modelServices || {}), task: result.task };
      renderModelServicesPanel();
      startModelTaskPolling();
      showToast(`任务已提交：${action === "start" ? "启动" : action === "stop" ? "停止" : "切换"} ${unit}`, "success");
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      state.modelServiceBusy = false;
    }
  }

  async function loadModelOverview(silent = false) {
    try {
      const data = await api("/api/model/overview");
      const hadData = Boolean(state.modelOverview);
      state.modelOverview = data;
      if (state.view !== "model") return;
      if (!silent || !hadData || !$(".model-status-band")) renderModel();
      else patchModel();
    } catch (error) {
      if (error.status !== 401) showToast(error.message, "error");
    }
  }

  async function loadModelLogs(silent = false) {
    if (state.modelLogsLoading) return;
    state.modelLogsLoading = true;
    try {
      const payload = await api(
        `/api/model/logs?rank=${encodeURIComponent(state.modelRank)}&lines=${encodeURIComponent(settingValue("ui_model_log_lines", 350))}`,
      );
      state.modelLogs = payload.lines || [];
      state.modelLogsUpdatedAt = Date.now();
      if (state.view === "model") renderModelLogs();
    } catch (error) {
      if (!silent && error.status !== 401) showToast(error.message, "error");
    } finally {
      state.modelLogsLoading = false;
    }
  }

  function renderModelLogs() {
    const output = $("#modelLogOutput");
    if (!output) return;
    const currentScroll = output.scrollTop;
    const atBottom = currentScroll + output.clientHeight >= output.scrollHeight - 20;
    const search = state.modelLogSearch.toLowerCase();
    const lines = state.modelLogs.filter((line) => !search || line.toLowerCase().includes(search));
    output.innerHTML = lines.length
      ? lines.map((line, index) => {
          const lower = line.toLowerCase();
          const level = /error|traceback|exception|cuda error/.test(lower)
            ? "error"
            : /\bwarn(?:ing)?\b/.test(lower)
              ? "warning"
              : "";
          return `
            <div class="model-log-line ${level}">
              <span class="line-number">${index + 1}</span>
              <span class="line-text">${escapeHtml(line)}</span>
            </div>
          `;
        }).join("")
      : '<div class="model-log-empty">没有匹配的日志</div>';
    output.scrollTop = atBottom ? output.scrollHeight : currentScroll;
  }

  function downloadModelLogs() {
    if (!state.modelLogs.length) {
      showToast("当前没有可下载的日志", "error");
      return;
    }
    const blob = new Blob([state.modelLogs.join("\n")], {
      type: "text/plain;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `spark-model-rank-${state.modelRank}-${formatClock(Date.now() / 1000).replaceAll(":", "")}.log`;
    link.click();
    URL.revokeObjectURL(url);
  }

  function renderModel() {
    const data = state.modelOverview;
    if (!data) {
      $("#content").innerHTML = `
        ${pageHead("推理服务运行态", "正在读取模型服务", `<button class="button secondary compact" data-action="model-refresh">${icon("refresh-cw")}<span>刷新</span></button>`, "model-overview")}
        <div class="panel"><div class="empty-state"><div class="empty-state-icon">${icon("loader-circle", "spin")}</div><strong>正在连接模型服务</strong></div></div>
      `;
      refreshIcons($("#content"));
      return;
    }
    const fields = Object.fromEntries(modelFieldDefinitions(data));
    const kpis = modelKpiDefinitions(data);
    const throughputCharts = modelThroughputChartDefinitions(data);
    const nodes = data.model?.nodes || [];
    const modelHistoryMinutes = Math.max(
      1,
      Number(data.throughput_history?.window_minutes || settingValue("model_history_minutes", 30)),
    );
    const modelHistoryBuckets = Math.max(
      2,
      Number(data.throughput_history?.bucket_seconds
        ? Math.round((modelHistoryMinutes * 60) / Number(data.throughput_history.bucket_seconds))
        : settingValue("model_history_buckets", 30)),
    );
    const modelPointInterval = (modelHistoryMinutes * 60) / modelHistoryBuckets;
    const statusClass = data.status === "degraded" ? "degraded" : data.status === "offline" ? "offline" : "";
    const rankOptions = [
      '<option value="all">全部节点</option>',
      ...nodes.map((node) => `<option value="${node.rank}" ${String(state.modelRank) === String(node.rank) ? "selected" : ""}>${escapeHtml(node.name)} · rank${node.rank}</option>`),
    ].join("");
    const kpiHtml = kpis.map((kpi) => `
      <div class="model-kpi ${kpi.tone === "primary" ? "accent" : ""} ${kpi.key === "tokens" ? "tokens" : ""}" data-model-kpi="${kpi.key}">
        <span>${escapeHtml(kpi.label)}</span>
        <strong><b data-kpi-value>${escapeHtml(kpi.value)}</b> <em>${escapeHtml(kpi.unit)}</em></strong>
        <small data-kpi-detail>${escapeHtml(kpi.detail)}</small>
      </div>
    `).join("");
    $("#content").innerHTML = `
      ${pageHead("推理服务运行态", `模型管理 · ${fields.health}`, `<button class="button secondary compact" data-action="model-refresh">${icon("refresh-cw")}<span>刷新</span></button>`, "model-overview")}
      <section class="model-status-band">
        <div class="model-identity">
          <div class="model-status-icon ${statusClass}">${icon("brain-circuit")}</div>
          <div class="model-identity-copy">
            <strong data-model-field="model-name">${escapeHtml(fields["model-name"])}</strong>
            <span><em class="model-engine-badge" data-model-field="engine">${escapeHtml(fields.engine)}</em> <b data-model-field="endpoint">${escapeHtml(fields.endpoint)}</b></span>
          </div>
        </div>
        <div class="model-usage-summary">
          <div class="model-usage-total">
            <span>启动后累计 · SINCE START</span>
            <strong data-model-field="usage-total">${escapeHtml(fields["usage-total"])}</strong>
            <small>启动 Started <b data-model-field="model-started">${escapeHtml(fields["model-started"])}</b></small>
          </div>
          <div class="model-usage-breakdown">
            <span>输入 Input <b data-model-field="usage-input">${escapeHtml(fields["usage-input"])}</b></span>
            <span>输出 Output <b data-model-field="usage-output">${escapeHtml(fields["usage-output"])}</b></span>
            <span>请求 Requests <b data-model-field="usage-requests">${escapeHtml(fields["usage-requests"])}</b></span>
          </div>
        </div>
        <div class="model-health-block">
          <div class="model-health-title"><span>节点健康</span><strong data-model-field="health">${escapeHtml(fields.health)}</strong></div>
          <div class="model-node-dots">
            ${nodes.map((node) => `<span class="model-node-dot ${node.healthy ? "healthy" : ""}" data-model-node="${node.rank}" title="${escapeHtml(node.name)} · ${escapeHtml(node.host)}"></span>`).join("")}
          </div>
        </div>
        <div class="model-container-state">
          <span class="model-state-pill ${statusClass}" data-model-field="status">${escapeHtml(fields.status)}</span>
          <span data-model-field="container-status">${escapeHtml(fields["container-status"])}</span>
        </div>
      </section>
      <div class="model-kpi-grid">${kpiHtml}</div>
      <div class="model-dashboard-grid">
        <section class="model-panel model-service-panel" id="modelServicePanel">${modelServicesPanelHtml()}</section>
        <section class="model-panel">
          <div class="model-panel-head"><h3>关键启动参数 <small>KEY STARTUP PARAMETERS</small>${helpButton("model-params")}</h3><span>HEAD / RANK 0</span></div>
          <div class="model-compact-list">
            <div class="model-param-row"><div><code>--kv-cache-memory-bytes</code><small>KV Cache memory allocation · KV 缓存显存分配</small></div><strong data-model-field="kv-cache-memory">${escapeHtml(fields["kv-cache-memory"])}</strong></div>
            <div class="model-param-row"><div><code>--gpu-memory-utilization</code><small>GPU memory usage ceiling · GPU 显存占用上限</small></div><strong data-model-field="gpu-memory-utilization">${escapeHtml(fields["gpu-memory-utilization"])}</strong></div>
            <div class="model-param-row"><div><code>--block-size</code><small>KV Cache block size · KV 缓存分块大小</small></div><strong data-model-field="block-size">${escapeHtml(fields["block-size"])}</strong></div>
            <div class="model-param-row"><div><code>--prefix-cache-retention-interval</code><small>Prefix cache retention interval · 前缀缓存保留间隔</small></div><strong data-model-field="prefix-cache-retention">${escapeHtml(fields["prefix-cache-retention"])}</strong></div>
            <div class="model-param-row"><div><code>--long-prefill-token-threshold</code><small>Long-prefill token threshold · 长 Prefill 判定阈值</small></div><strong data-model-field="long-prefill-threshold">${escapeHtml(fields["long-prefill-threshold"])}</strong></div>
            <div class="model-param-row"><div><code>--max-num-seqs</code><small>Maximum concurrent sequences · 单批最大并发序列数</small></div><strong data-model-field="max-num-seqs">${escapeHtml(fields["max-num-seqs"])}</strong></div>
          </div>
        </section>
        <section class="model-panel">
          <div class="model-panel-head"><h3>集群状态 <small>CLUSTER STATUS</small>${helpButton("model-cluster")}</h3><span>${nodes.length} NODE</span></div>
          <div class="model-compact-list">
            <div class="model-compact-row"><span>节点健康</span><strong data-model-field="health">${escapeHtml(fields.health)}</strong></div>
            <div class="model-compact-row"><span>服务状态</span><strong data-model-field="status">${escapeHtml(fields.status)}</strong></div>
            <div class="model-compact-row"><span>GPU 负载</span><strong data-model-field="cluster-gpu">${escapeHtml(fields["cluster-gpu"])}</strong></div>
            <div class="model-compact-row"><span>GPU 温度</span><strong data-model-field="cluster-temp">${escapeHtml(fields["cluster-temp"])}</strong></div>
            <div class="model-compact-row"><span>统一内存</span><strong data-model-field="cluster-memory">${escapeHtml(fields["cluster-memory"])}</strong></div>
            <div class="model-compact-row"><span>网络吞吐</span><strong data-model-field="cluster-network">${escapeHtml(fields["cluster-network"])}</strong></div>
          </div>
        </section>
        <section class="model-panel model-throughput-panel">
          <div class="model-throughput-bars">
            ${throughputCharts.map((item) => `
              <div class="model-throughput-item" data-model-throughput="${item.key}">
                <div class="model-throughput-label">
                  <span>${escapeHtml(item.label)}</span>
                  <strong><b data-throughput-value>${escapeHtml(item.value)}</b> <em>tok/s</em></strong>
                </div>
                <div class="model-throughput-scale">峰值 ${escapeHtml(item.peak)} · ${escapeHtml(item.detail)}</div>
                <div class="model-throughput-plot">
                  <canvas data-throughput-canvas aria-label="${escapeHtml(item.label)} 最近 ${escapeHtml(formatHistoryRange(modelHistoryMinutes))}吞吐曲线"></canvas>
                  <div class="model-throughput-tooltip" data-throughput-tooltip hidden></div>
                </div>
                <div class="model-throughput-axis">
                  <span>${escapeHtml(item.points[0]?.label || "—")}</span>
                  <span>${escapeHtml(item.points[Math.floor(item.points.length / 2)]?.label || "—")}</span>
                  <span>${escapeHtml(item.points[item.points.length - 1]?.label || "—")}</span>
                </div>
              </div>
            `).join("")}
          </div>
        </section>
      </div>
      <div class="model-event-strip">
        <div class="model-event-item"><span>最近阶段</span><strong data-model-field="last-batch">${escapeHtml(fields["last-batch"])}</strong></div>
        <div class="model-event-item"><span>最近 Prefill</span><strong data-model-field="last-prefill">${escapeHtml(fields["last-prefill"])}</strong></div>
        <div class="model-event-item"><span>最近 Decode</span><strong data-model-field="last-decode">${escapeHtml(fields["last-decode"])}</strong></div>
        <div class="model-event-item"><span>TTFT</span><strong data-model-field="ttft">${escapeHtml(fields.ttft)}</strong></div>
        <div class="model-event-item"><span>E2E</span><strong data-model-field="e2e">${escapeHtml(fields.e2e)}</strong></div>
        <div class="model-event-item"><span>ITL</span><strong data-model-field="inter-token">${escapeHtml(fields["inter-token"])}</strong></div>
        <div class="model-event-item"><span>成功 / 失败</span><strong><span data-model-field="success-requests">${escapeHtml(fields["success-requests"])}</span> / <span data-model-field="failed-requests">${escapeHtml(fields["failed-requests"])}</span></strong></div>
        <div class="model-event-item"><span>日志错误</span><strong data-model-field="log-errors">${escapeHtml(fields["log-errors"])}</strong></div>
      </div>
      <section class="model-log-panel">
        <div class="model-log-toolbar">
          <div class="model-log-title">${icon("terminal")}<span>模型运行日志</span>${helpButton("model-logs")}</div>
          <div class="model-log-actions">
            <select class="select" id="modelLogRank">${rankOptions}</select>
            <input class="text-input" id="modelLogSearch" value="${escapeHtml(state.modelLogSearch)}" placeholder="筛选日志">
            <button class="button ghost compact" data-action="model-log-refresh">${icon("refresh-cw")}<span>刷新</span></button>
            <button class="button ghost compact" data-action="model-log-download">${icon("download")}<span>下载</span></button>
          </div>
        </div>
        <div class="model-log-output" id="modelLogOutput"></div>
      </section>
    `;
    renderModelLogs();
    refreshIcons($("#content"));
    requestAnimationFrame(drawModelThroughputCharts);
  }

  function patchModel() {
    if (!state.modelOverview) return;
    const fields = Object.fromEntries(modelFieldDefinitions(state.modelOverview));
    for (const element of $$("[data-model-field]")) {
      const key = element.dataset.modelField;
      if (key in fields && element.textContent !== fields[key]) {
        element.textContent = fields[key];
      }
    }
    const statusClass =
      state.modelOverview.status === "degraded"
        ? "degraded"
        : state.modelOverview.status === "offline"
          ? "offline"
          : "";
    const iconElement = $(".model-status-icon");
    if (iconElement) iconElement.className = `model-status-icon ${statusClass}`;
    const pill = $(".model-state-pill");
    if (pill) pill.className = `model-state-pill ${statusClass}`;
    for (const kpi of modelKpiDefinitions(state.modelOverview)) {
      const card = $(`[data-model-kpi="${kpi.key}"]`);
      if (!card) continue;
      const value = $("[data-kpi-value]", card);
      const detail = $("[data-kpi-detail]", card);
      if (value && value.textContent !== kpi.value) value.textContent = kpi.value;
      if (detail && detail.textContent !== kpi.detail) detail.textContent = kpi.detail;
    }
    for (const node of state.modelOverview.model?.nodes || []) {
      const dot = $(`[data-model-node="${node.rank}"]`);
      if (dot) dot.classList.toggle("healthy", node.healthy);
    }
    for (const item of modelThroughputChartDefinitions(state.modelOverview)) {
      const row = $(`[data-model-throughput="${item.key}"]`);
      if (!row) continue;
      const value = $("[data-throughput-value]", row);
      const scale = $(".model-throughput-scale", row);
      const axisLabels = $$(".model-throughput-axis span", row);
      if (value) value.textContent = item.value;
      if (scale) scale.textContent = `峰值 ${item.peak} · ${item.detail}`;
      if (axisLabels.length === 3) {
        axisLabels[0].textContent = item.points[0]?.label || "—";
        axisLabels[1].textContent =
          item.points[Math.floor(item.points.length / 2)]?.label || "—";
        axisLabels[2].textContent =
          item.points[item.points.length - 1]?.label || "—";
      }
    }
    drawModelThroughputCharts();
  }

  function settingFieldHtml(field, rawValue) {
    const value = rawValue ?? field.value ?? field.default;
    const isBoolean = field.type === "bool";
    const isNumeric = field.type === "int" || field.type === "float";
    const isList = field.type === "list_int" || field.type === "list_str";
    const isWide = isList || Number(field.max_length || 0) > 160;
    const inputValue = isList
      ? Array.isArray(value)
        ? value.join(", ")
        : String(value ?? "")
      : isBoolean
        ? value
        : String(value ?? "");
    const range =
      field.min != null && field.max != null
        ? `${formatNumber(field.min, field.type === "float" ? 2 : 0)} - ${formatNumber(field.max, field.type === "float" ? 2 : 0)}`
        : "";
    const meta = [field.unit, range].filter(Boolean).join(" · ");
    const control = isBoolean
      ? `
        <label class="setting-toggle-control" for="setting-${escapeHtml(field.key)}">
          <input id="setting-${escapeHtml(field.key)}" class="setting-checkbox" name="${escapeHtml(field.key)}" type="checkbox" ${value ? "checked" : ""}>
          <span>${value ? "已启用" : "已关闭"}</span>
        </label>
      `
      : `
        <input
          id="setting-${escapeHtml(field.key)}"
          class="setting-input"
          name="${escapeHtml(field.key)}"
          type="${isNumeric ? "number" : "text"}"
          value="${escapeHtml(inputValue)}"
          ${isNumeric ? `min="${field.min ?? ""}" max="${field.max ?? ""}" step="${field.type === "float" ? "any" : "1"}" required` : ""}
          ${field.max_length ? `maxlength="${Number(field.max_length)}"` : ""}
          ${field.placeholder ? `placeholder="${escapeHtml(field.placeholder)}"` : ""}
        >
      `;
    return `
      <div class="setting-field ${isWide ? "is-wide" : ""} ${isBoolean ? "is-toggle" : ""}">
        <span class="setting-label-row">
          <strong>${escapeHtml(field.label)}</strong>
          ${meta ? `<small>${escapeHtml(meta)}</small>` : ""}
        </span>
        <span class="setting-description">${escapeHtml(field.description)}</span>
        ${control}
      </div>
    `;
  }

  function renderSettings() {
    const settings = state.settings || state.dashboard?.thresholds || {};
    const summary = state.dashboard?.summary || {};
    const process = state.settingsProcess || {};
    const groups = state.settingsGroups.filter((group) => group.fields?.length);
    const groupHtml = groups.map((group) => `
      <section class="settings-group" data-settings-group="${escapeHtml(group.key)}">
        <div class="settings-card-head">
          <h3>${escapeHtml(group.label)}</h3>
          <p>${escapeHtml(group.description)}</p>
        </div>
        <div class="settings-fields">
          ${group.fields.map((field) => settingFieldHtml(field, settings[field.key])).join("")}
        </div>
      </section>
    `).join("");
    $("#content").innerHTML = `
      ${pageHead(
        "系统设置",
        "所有运行参数均由当前页面管理，保存后自动应用到后续采集和刷新。",
        `<button class="button secondary compact" type="submit" form="settingsForm">${icon("save")}<span>保存设置</span></button>`,
        "settings",
      )}
      <form id="settingsForm" class="settings-layout">
        <div class="settings-groups">${groupHtml}</div>
        <aside class="settings-side">
          <section class="settings-card">
            <div class="settings-card-head">
              <h3>控制面</h3>
              <p>当前会话与本机运行状态。</p>
            </div>
            <div class="settings-body">
              <div class="info-list">
                <div class="info-row"><span>管理员</span><strong>${escapeHtml(state.user?.username || "admin")}</strong></div>
                <div class="info-row"><span>在线节点</span><strong>${summary.online || 0} / ${summary.total || 0}</strong></div>
                <div class="info-row"><span>采集间隔</span><strong>${escapeHtml(formatNumber(settingValue("poll_interval", 3), 1))} 秒</strong></div>
                <div class="info-row"><span>会话有效期</span><strong>${escapeHtml(formatNumber(settingValue("session_ttl_hours", 12)))} 小时</strong></div>
                <div class="info-row"><span>控制面版本</span><strong>${escapeHtml(state.version || "—")}</strong></div>
                <div class="info-row"><span>监听地址</span><strong class="path-value">${escapeHtml(`${process.host || "—"}:${process.port || "—"}`)}</strong></div>
                <div class="info-row"><span>数据目录</span><strong class="path-value">${escapeHtml(process.data_dir || "—")}</strong></div>
                <div class="info-row"><span>数据库文件</span><strong class="path-value">${escapeHtml(process.database_path || "—")}</strong></div>
              </div>
              <div class="security-note">${icon("shield-check")}<span>会话 Cookie 使用 HttpOnly 与 SameSite=Strict；所有写操作同时校验 CSRF Token；SSH 密码以加密形式保存。</span></div>
              <div class="settings-actions"><button class="primary-button compact" type="submit">${icon("save")}<span>保存设置</span></button></div>
            </div>
          </section>
        </aside>
      </form>
    `;
  }

  function findNode(id) {
    return (state.dashboard?.nodes || []).find((node) => Number(node.id) === Number(id));
  }

  function openDrawer(nodeId) {
    const id = Number(nodeId);
    if (state.historyNodeId !== id) {
      state.historyNodeId = id;
      const configuredDefault = Number(settingValue("ui_history_minutes", 30));
      state.historyRange = configuredHistoryRanges().includes(configuredDefault)
        ? configuredDefault
        : configuredHistoryRanges()[0];
      state.historyData = null;
    }
    state.drawerNodeId = id;
    const node = findNode(id);
    if (node) renderDrawer(node);
    loadNodeHistory(id, state.historyRange);
  }

  async function loadNodeHistory(nodeId, minutes) {
    state.historyLoading = true;
    if (state.drawerNodeId === nodeId) {
      const node = findNode(nodeId);
      if (node) updateHistoryPanel(node);
    }
    try {
      const payload = await api(`/api/nodes/${nodeId}/history?minutes=${minutes}`);
      if (state.historyNodeId !== nodeId || state.historyRange !== minutes) return;
      state.historyData = Array.isArray(payload.series) ? payload.series : [];
    } catch (error) {
      if (state.historyNodeId === nodeId) state.historyData = [];
      showToast(error.message, "error");
    } finally {
      if (state.historyNodeId !== nodeId || state.historyRange !== minutes) return;
      state.historyLoading = false;
      if (state.drawerNodeId === nodeId) {
        const node = findNode(nodeId);
        if (node) updateHistoryPanel(node);
      }
    }
  }

  function updateHistoryPanel(node) {
    const root = $("#drawerRoot");
    if (!root || !$(".drawer", root) || state.historyNodeId !== Number(node.id)) return;
    for (const button of $$("[data-action='history-range']", root)) {
      button.classList.toggle(
        "active",
        Number(button.dataset.minutes) === Number(state.historyRange),
      );
    }
    const history =
      Array.isArray(state.historyData) && state.historyNodeId === Number(node.id)
        ? state.historyData
        : [];
    const status = $(".detail-chart-title small", root);
    if (status) {
      status.textContent = state.historyLoading
        ? "正在加载"
        : history.length
          ? `${history.length} 个采样点`
          : "暂无历史数据";
    }
    const wrap = $(".detail-chart-wrap", root);
    let empty = $(".chart-empty", wrap);
    if (!history.length && !state.historyLoading) {
      if (!empty) {
        empty = document.createElement("div");
        empty.className = "chart-empty";
        empty.textContent = "当前时间范围内没有历史数据";
        wrap?.appendChild(empty);
      }
      empty.hidden = false;
    } else if (empty) {
      empty.hidden = true;
    }
    drawHistoryChart($("#detailChart"), history);
  }

  function changeHistoryRange(nodeId, minutes) {
    if (state.historyRange === minutes && state.historyData) return;
    state.historyRange = minutes;
    state.historyData = null;
    const node = findNode(nodeId);
    if (node) updateHistoryPanel(node);
    loadNodeHistory(nodeId, minutes);
  }

  function drawerNetwork(node) {
    const networkMap = new Map();
    for (const item of node.latest_metrics?.network || []) {
      if (!item.name || item.rx_bytes == null) continue;
      if (!networkMap.has(item.name)) {
        networkMap.set(item.name, { ...item, addresses: [item.address].filter(Boolean) });
      } else if (item.address) {
        networkMap.get(item.name).addresses.push(item.address);
      }
    }
    return Array.from(networkMap.values());
  }

  function drawerNetworkHtml(network) {
    return `
      <div class="detail-network-head"><span>网络接口</span><span>实时速率</span></div>
      ${network.length ? network.map((item) => `
        <div class="detail-network-row" data-network-row="${escapeHtml(item.name)}">
          <div>
            <div class="detail-network-name"><strong data-network-name>${escapeHtml(item.name)}</strong>${item.kind === "rdma" ? '<em class="network-kind">RDMA</em>' : ""}</div>
            <span data-network-address>${escapeHtml(item.addresses.join(" · ") || "无地址")}</span>
          </div>
          <div><span data-network-rx>RX ${formatThroughput(item.rx_rate)}</span><span data-network-tx>TX ${formatThroughput(item.tx_rate)}</span></div>
        </div>
      `).join("") : '<div class="detail-network-empty">没有可用的网络计数</div>'}
    `;
  }

  function patchDrawerNetwork(container, network) {
    if (!container) return;
    const rows = Array.from(container.querySelectorAll("[data-network-row]"));
    const currentNames = rows.map((row) => row.dataset.networkRow);
    const nextNames = network.map((item) => item.name);
    if (
      currentNames.length !== nextNames.length ||
      currentNames.some((name, index) => name !== nextNames[index])
    ) {
      container.innerHTML = drawerNetworkHtml(network);
      return;
    }
    for (const item of network) {
      const row = container.querySelector(`[data-network-row="${CSS.escape(item.name)}"]`);
      if (!row) continue;
      const updates = [
        ["[data-network-name]", item.name],
        ["[data-network-address]", item.addresses.join(" · ") || "无地址"],
        ["[data-network-rx]", `RX ${formatThroughput(item.rx_rate)}`],
        ["[data-network-tx]", `TX ${formatThroughput(item.tx_rate)}`],
      ];
      for (const [selector, value] of updates) {
        const element = $(selector, row);
        if (element && element.textContent !== value) element.textContent = value;
      }
    }
  }

  function renderDrawer(node, preserveScroll = false) {
    const root = $("#drawerRoot");
    const previousScroll = preserveScroll ? $(".drawer", root)?.scrollTop || 0 : 0;
    const { resources, gpus, gpu } = nodeMetrics(node);
    const firstGpu = gpus[0] || {};
    const memoryTotal = Number(resources.memory_total || 0);
    const memoryUsed = Number(resources.memory_used || 0);
    const memoryAvailable = Number(resources.memory_available || 0);
    const memoryCached = Number(resources.memory_cached || 0);
    const history =
      state.historyNodeId === Number(node.id) && Array.isArray(state.historyData)
        ? state.historyData
        : [];
    const rangeOptions = configuredHistoryRanges().map((minutes) => [
      minutes,
      formatHistoryRange(minutes),
    ]);
    const historyStatus = state.historyLoading
      ? "正在加载"
      : history.length
        ? `${history.length} 个采样点`
        : "暂无历史数据";
    const network = drawerNetwork(node);
    root.innerHTML = `
      <div class="drawer-backdrop" data-action="close-drawer"></div>
      <aside class="drawer" aria-label="${escapeHtml(node.name)} 详情">
        <div class="drawer-head">
          <div class="drawer-title">
            <span class="node-status ${node.status}" data-drawer-status></span>
            <div><strong>${escapeHtml(node.name)}</strong><span data-drawer-subtitle>${escapeHtml(sshAddress(node.ssh_user, node.host, node.ssh_port))} · ${statusLabel(node.status)}</span></div>
          </div>
          <div class="drawer-actions">
            <button class="icon-button subtle" data-action="refresh-node" data-id="${node.id}" title="刷新" aria-label="刷新">${icon("refresh-cw")}</button>
            <button class="icon-button subtle" data-action="edit-node" data-id="${node.id}" title="编辑" aria-label="编辑">${icon("pencil")}</button>
            <button class="icon-button subtle" data-action="close-drawer" title="关闭" aria-label="关闭">${icon("x")}</button>
          </div>
        </div>
        <div class="drawer-body">
          <div class="drawer-hero">
            <div class="drawer-stat"><span>${escapeHtml(gpuLoadLabel(firstGpu))}</span><strong><span data-drawer-stat="gpu">${formatNumber(gpu, 1)}</span><small>%</small></strong></div>
            <div class="drawer-stat"><span>UNIFIED MEMORY</span><strong><span data-drawer-stat="memory">${formatNumber(resources.memory_percent, 1)}</span><small>%</small></strong></div>
            <div class="drawer-stat"><span>CPU UTILIZATION</span><strong><span data-drawer-stat="cpu">${formatNumber(resources.cpu_percent, 1)}</span><small>%</small></strong></div>
            <div class="drawer-stat"><span>GPU TEMPERATURE</span><strong><span data-drawer-stat="temperature">${formatNumber(resources.temperature_max, 0)}</span><small>°C</small></strong></div>
          </div>
          <div class="detail-chart-card">
            <div class="detail-chart-head">
              <div class="detail-chart-title"><span>历史曲线</span><small>${historyStatus}</small></div>
              <div class="history-range-group">
                ${rangeOptions.map(([minutes, label]) => `<button class="${state.historyRange === minutes ? "active" : ""}" data-action="history-range" data-id="${node.id}" data-minutes="${minutes}">${label}</button>`).join("")}
              </div>
            </div>
            <div class="detail-chart-legend">
              <div class="chart-legend"><span><i></i>CPU</span><span><i class="amber"></i>GPU</span><span><i class="blue"></i>内存</span></div>
            </div>
            <div class="detail-chart-wrap">
              <canvas class="detail-chart" id="detailChart"></canvas>
              <div class="chart-tooltip" id="chartTooltip" hidden></div>
              ${!history.length && !state.historyLoading ? '<div class="chart-empty">当前时间范围内没有历史数据</div>' : ""}
            </div>
          </div>
          <div class="detail-grid">
            <div class="detail-grid-item"><span>GPU</span><strong data-drawer-detail="gpu-name">${escapeHtml(firstGpu.name || gpuFallbackName())}</strong></div>
            <div class="detail-grid-item"><span>GPU 负载口径</span><strong data-drawer-detail="gpu-source">${firstGpu.load_source === "power estimate" ? "功耗 + P-State 估算" : "驱动利用率"}</strong></div>
            <div class="detail-grid-item"><span>GPU 功耗</span><strong data-drawer-detail="gpu-power">${firstGpu.power != null ? `${formatNumber(firstGpu.power, 0)} W` : "—"}</strong></div>
            <div class="detail-grid-item"><span>GPU 温度</span><strong data-drawer-detail="gpu-temp">${firstGpu.temperature != null ? `${formatNumber(firstGpu.temperature, 0)}°C` : "—"}</strong></div>
            <div class="detail-grid-item"><span>P-State / 图形时钟</span><strong data-drawer-detail="gpu-clock">${escapeHtml(firstGpu.pstate || "—")} · ${firstGpu.clock_graphics != null ? `${formatNumber(firstGpu.clock_graphics, 0)} MHz` : "—"}</strong></div>
            <div class="detail-grid-item"><span>统一内存</span><strong data-drawer-detail="memory">${formatBytes(memoryUsed)} / ${formatBytes(memoryTotal)}</strong></div>
            <div class="detail-grid-item"><span>可用内存 / 缓存</span><strong data-drawer-detail="memory-extra">${formatBytes(memoryAvailable)} / ${formatBytes(memoryCached)}</strong></div>
            <div class="detail-grid-item"><span>系统负载</span><strong data-drawer-detail="load">${formatNumber(resources.load_1, 2)} / ${formatNumber(resources.load_5, 2)} / ${formatNumber(resources.load_15, 2)}</strong></div>
            <div class="detail-grid-item"><span>CPU 核心 / 采样口径</span><strong data-drawer-detail="cpu-core">${formatNumber(resources.cpu_count)} 核 · 全核平均</strong></div>
            <div class="detail-grid-item"><span>网络下行</span><strong data-drawer-detail="rx">${formatThroughput(resources.rx_rate)}</strong></div>
            <div class="detail-grid-item"><span>网络上行</span><strong data-drawer-detail="tx">${formatThroughput(resources.tx_rate)}</strong></div>
            <div class="detail-grid-item"><span>根分区</span><strong data-drawer-detail="disk">${formatBytes(resources.disk_used)} / ${formatBytes(resources.disk_total)}</strong></div>
            <div class="detail-grid-item"><span>运行时间</span><strong data-drawer-detail="uptime">${formatDuration(resources.uptime)}</strong></div>
            <div class="detail-grid-item"><span>进程数</span><strong data-drawer-detail="processes">${formatNumber(resources.process_count)}</strong></div>
            <div class="detail-grid-item"><span>采集延迟</span><strong data-drawer-detail="latency">${formatNumber(node.last_latency_ms, 0)} ms</strong></div>
            <div class="detail-grid-item"><span>分组 / 标签</span><strong data-drawer-detail="identity">${escapeHtml(node.group_name || defaultGroupName())} · ${escapeHtml((node.tags || []).join(" / ") || "—")}</strong></div>
            <div class="detail-grid-item"><span>最近采集</span><strong data-drawer-detail="heartbeat">${formatDateTime(node.last_seen)}</strong></div>
            <div class="detail-grid-item"><span>SSH 连接</span><strong data-drawer-detail="ssh">${escapeHtml(sshAddress(node.ssh_user, node.host, node.ssh_port))}</strong></div>
          </div>
          <section class="detail-network" data-drawer-network>
            ${drawerNetworkHtml(network)}
          </section>
        </div>
      </aside>
    `;
    refreshIcons(root);
    const drawer = $(".drawer", root);
    if (drawer) drawer.scrollTop = previousScroll;
    drawHistoryChart($("#detailChart"), history);
  }

  function patchDrawer(node) {
    const root = $("#drawerRoot");
    if (!root || !$(".drawer", root)) return;
    const { resources, gpus, gpu } = nodeMetrics(node);
    const firstGpu = gpus[0] || {};
    const setDetail = (key, value) => {
      const element = $(`[data-drawer-detail="${key}"]`, root);
      if (element && element.textContent !== value) element.textContent = value;
    };
    const statusDot = $("[data-drawer-status]", root);
    if (statusDot) statusDot.className = `node-status ${node.status}`;
    const subtitle = $("[data-drawer-subtitle]", root);
    if (subtitle) {
      subtitle.textContent = `${sshAddress(node.ssh_user, node.host, node.ssh_port)} · ${statusLabel(node.status)}`;
    }
    const title = $(".drawer-title strong", root);
    if (title) title.textContent = node.name;
    const stats = {
      gpu: formatNumber(gpu, 1),
      memory: formatNumber(resources.memory_percent, 1),
      cpu: formatNumber(resources.cpu_percent, 1),
      temperature: formatNumber(resources.temperature_max, 0),
    };
    for (const [key, value] of Object.entries(stats)) {
      const element = $(`[data-drawer-stat="${key}"]`, root);
      if (element && element.textContent !== value) element.textContent = value;
    }
    setDetail("gpu-name", firstGpu.name || gpuFallbackName());
    setDetail(
      "gpu-source",
      firstGpu.load_source === "power estimate" ? "功耗 + P-State 估算" : "驱动利用率",
    );
    setDetail("gpu-power", firstGpu.power != null ? `${formatNumber(firstGpu.power, 0)} W` : "—");
    setDetail("gpu-temp", firstGpu.temperature != null ? `${formatNumber(firstGpu.temperature, 0)}°C` : "—");
    setDetail(
      "gpu-clock",
      `${firstGpu.pstate || "—"} · ${firstGpu.clock_graphics != null ? `${formatNumber(firstGpu.clock_graphics, 0)} MHz` : "—"}`,
    );
    setDetail(
      "memory",
      `${formatBytes(resources.memory_used)} / ${formatBytes(resources.memory_total)}`,
    );
    setDetail(
      "memory-extra",
      `${formatBytes(resources.memory_available)} / ${formatBytes(resources.memory_cached)}`,
    );
    setDetail(
      "load",
      `${formatNumber(resources.load_1, 2)} / ${formatNumber(resources.load_5, 2)} / ${formatNumber(resources.load_15, 2)}`,
    );
    setDetail(
      "cpu-core",
      `${formatNumber(resources.cpu_count)} 核 · 全核平均`,
    );
    setDetail("rx", formatThroughput(resources.rx_rate));
    setDetail("tx", formatThroughput(resources.tx_rate));
    setDetail("disk", `${formatBytes(resources.disk_used)} / ${formatBytes(resources.disk_total)}`);
    setDetail("uptime", formatDuration(resources.uptime));
    setDetail("processes", formatNumber(resources.process_count));
    setDetail("latency", `${formatNumber(node.last_latency_ms, 0)} ms`);
    setDetail("identity", `${node.group_name || defaultGroupName()} · ${(node.tags || []).join(" / ") || "—"}`);
    setDetail("heartbeat", formatDateTime(node.last_seen));
    setDetail("ssh", sshAddress(node.ssh_user, node.host, node.ssh_port));
    const network = $("[data-drawer-network]", root);
    patchDrawerNetwork(network, drawerNetwork(node));
  }

  function closeDrawer() {
    state.drawerNodeId = null;
    state.historyNodeId = null;
    state.historyData = null;
    state.historyLoading = false;
    $("#drawerRoot").innerHTML = "";
  }

  function emptyState(iconName, title, copy, actionLabel = "", action = "") {
    const actionHtml = actionLabel
      ? `<button class="button secondary compact" data-action="${escapeHtml(action)}">${icon(iconName === "radar" ? "radar" : "plus")}<span>${escapeHtml(actionLabel)}</span></button>`
      : "";
    return `
      <div class="empty-state">
        <div class="empty-state-icon">${icon(iconName)}</div>
        <strong>${escapeHtml(title)}</strong>
        <p>${escapeHtml(copy)}</p>
        ${actionHtml}
      </div>
    `;
  }

  function openModal(html) {
    $("#modalRoot").innerHTML = `<div class="modal-backdrop" data-action="modal-backdrop"><div role="dialog" aria-modal="true">${html}</div></div>`;
    refreshIcons($("#modalRoot"));
    const firstInput = $("input:not([type=hidden]), textarea, select", $("#modalRoot"));
    window.setTimeout(() => firstInput?.focus(), 50);
  }

  function closeModal() {
    state.confirmAction = null;
    $("#modalRoot").innerHTML = "";
  }

  function confirmDialog(title, message, confirmLabel, onConfirm, danger = false) {
    state.confirmAction = onConfirm;
    openModal(`
      <div class="modal">
        <div class="modal-head"><h3>${escapeHtml(title)}</h3><button class="icon-button subtle" data-action="close-modal" title="关闭" aria-label="关闭">${icon("x")}</button></div>
        <div class="modal-body"><p class="confirm-copy">${escapeHtml(message)}</p></div>
        <div class="modal-foot">
          <button class="button secondary" data-action="close-modal">取消</button>
          <button class="button ${danger ? "danger" : "primary-button"}" data-action="confirm-modal">${escapeHtml(confirmLabel)}</button>
        </div>
      </div>
    `);
  }

  function openBulkAddModal() {
    openModal(`
      <div class="modal wide">
        <div class="modal-head"><h3>批量添加节点</h3><button class="icon-button subtle" data-action="close-modal" title="关闭" aria-label="关闭">${icon("x")}</button></div>
        <form id="bulkAddForm">
          <div class="modal-body">
            <div class="form-grid">
              <label class="field full"><span>节点地址</span><textarea class="textarea" name="input" required placeholder="192.0.2.11&#10;192.0.2.12,spark2,G1&#10;192.0.2.13-18"></textarea><span class="field-hint">每行支持 IP、主机名、CIDR 或尾段范围；可附带名称、分组和标签。</span></label>
              <label class="field"><span>默认分组</span><input class="text-input" name="group_name" value="${escapeHtml(defaultGroupName())}"></label>
              <label class="field"><span>默认标签</span><input class="text-input" name="tags" placeholder="spark, lan"></label>
              ${sshCredentialFields({
                passwordHint: "密码会加密保存；也可以只填写私钥路径。",
              })}
            </div>
            <div id="modalError" class="form-error modal-error" hidden></div>
          </div>
          <div class="modal-foot"><button class="button secondary" type="button" data-action="close-modal">取消</button><button class="primary-button" type="submit">${icon("plus")}<span>开始纳管</span></button></div>
        </form>
      </div>
    `);
  }

  function openEditNodeModal(nodeId) {
    const node = findNode(nodeId);
    if (!node) return;
    openModal(`
      <div class="modal wide">
        <div class="modal-head"><h3>编辑节点</h3><button class="icon-button subtle" data-action="close-modal" title="关闭" aria-label="关闭">${icon("x")}</button></div>
        <form id="editNodeForm" data-id="${node.id}">
          <div class="modal-body">
            <div class="form-grid">
              <label class="field"><span>节点名称</span><input class="text-input" name="name" value="${escapeHtml(node.name)}" required></label>
              <label class="field"><span>节点地址</span><input class="text-input" name="host" value="${escapeHtml(node.host)}" required></label>
              <label class="field"><span>分组</span><input class="text-input" name="group_name" value="${escapeHtml(node.group_name || defaultGroupName())}"></label>
              <label class="field"><span>标签</span><input class="text-input" name="tags" value="${escapeHtml((node.tags || []).join(", "))}"></label>
              ${sshCredentialFields({
                user: node.ssh_user,
                port: node.ssh_port,
                keyPath: node.ssh_key_path,
                passwordConfigured: Boolean(node.ssh_password_configured),
                allowClearPassword: true,
                passwordHint: "留空保持已保存密码；填写新密码会替换旧密码。",
              })}
            </div>
            <div id="modalError" class="form-error modal-error" hidden></div>
          </div>
          <div class="modal-foot">
            <button class="button ghost" type="button" data-action="test-node" data-id="${node.id}">${icon("plug-zap")}<span>测试已保存连接</span></button>
            <span class="toolbar-spacer"></span>
            <button class="button secondary" type="button" data-action="close-modal">取消</button>
            <button class="primary-button" type="submit">${icon("save")}<span>保存修改</span></button>
          </div>
        </form>
      </div>
    `);
  }

  function openBatchTextModal(kind) {
    const group = kind === "group";
    openModal(`
      <div class="modal">
        <div class="modal-head"><h3>${group ? "设置节点分组" : "设置节点标签"}</h3><button class="icon-button subtle" data-action="close-modal" title="关闭" aria-label="关闭">${icon("x")}</button></div>
        <form id="batchTextForm" data-kind="${kind}">
          <div class="modal-body">
            <label class="field"><span>${group ? "分组名称" : "标签"}</span><input class="text-input" name="value" required placeholder="${group ? "例如 G1" : "例如 inference, high-priority"}"></label>
          </div>
          <div class="modal-foot"><button class="button secondary" type="button" data-action="close-modal">取消</button><button class="primary-button" type="submit">应用到 ${state.selectedNodes.size} 个节点</button></div>
        </form>
      </div>
    `);
  }

  async function openScanModal() {
    if (!state.discovery) await loadDiscovery(true);
    const interfaces = state.discovery?.interfaces || [];
    if (state.scanInterface && !interfaces.some((item) => item.name === state.scanInterface)) {
      state.scanInterface = "";
    }
    const options = [
      `<option value="" ${state.scanInterface ? "" : "selected"}>${interfaces.length ? "自动选择全部可用网卡" : "未检测到 IPv4 网卡"}</option>`,
      ...interfaces.map((item) => `
        <option value="${escapeHtml(item.name)}" ${state.scanInterface === item.name ? "selected" : ""}>
          ${escapeHtml(item.name)} · ${escapeHtml(item.address)} · ${escapeHtml(item.cidr)}${item.physical ? "" : " · 虚拟"}
        </option>
      `),
    ].join("");
    openModal(`
      <div class="modal wide">
        <div class="modal-head"><h3>扫描局域网</h3><button class="icon-button subtle" data-action="close-modal" title="关闭" aria-label="关闭">${icon("x")}</button></div>
        <form id="scanModalForm">
          <div class="modal-body">
            <div class="form-grid">
              <label class="field full"><span>扫描网卡</span><select class="select" name="interface">${options}</select><span class="field-hint">扫描范围会按所选网卡的 IPv4 网段自动生成。</span></label>
              <label class="field full"><span>CIDR 覆盖</span><input class="text-input" name="cidrs" placeholder="留空使用所选网卡网段"></label>
              ${sshCredentialFields({
                passwordHint: "密码只用于本次扫描和认证成功后的加密保存；留空时优先复用已保存凭据。",
              })}
            </div>
          </div>
          <div class="modal-foot"><button class="button secondary" type="button" data-action="close-modal">取消</button><button class="primary-button" type="submit">${icon("scan-line")}<span>开始扫描</span></button></div>
        </form>
      </div>
    `);
  }

  function readSshCredentials(data) {
    const credentials = {
      ssh_user: String(data.get("ssh_user") || "").trim(),
      ssh_key_path: String(data.get("ssh_key_path") || "").trim(),
    };
    const portValue = String(data.get("ssh_port") || "").trim();
    if (portValue) credentials.ssh_port = Number(portValue);
    const password = String(data.get("ssh_password") || "");
    if (password) credentials.ssh_password = password;
    return credentials;
  }

  async function startScan(interfaceName = "", cidrs = "", credentials = {}) {
    try {
      const list = cidrs
        .split(/[,;\s]+/)
        .map((item) => item.trim())
        .filter(Boolean);
      const payload = await api("/api/discovery/scan", {
        method: "POST",
        body: { interface: interfaceName || null, cidrs: list, ...credentials },
      });
      state.scanInterface = interfaceName || "";
      state.discovery = {
        ...(state.discovery || { discovered: [], interfaces: [] }),
        scan: payload.scan,
      };
      closeModal();
      showToast("局域网扫描已启动");
      navigate("discovery");
      window.setTimeout(() => loadDiscovery(), 900);
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function testNodeConnection(nodeId, button) {
    setButtonBusy(button, true, "测试中");
    try {
      const result = await api(`/api/nodes/${nodeId}/test`, {
        method: "POST",
        body: {},
      });
      const latency = Number(result.latency_ms);
      const target = String(result.hostname || result.host || "").trim();
      showToast(
        `SSH 连接成功${target ? `：${target}` : ""}${Number.isFinite(latency) ? `，${Math.round(latency)} ms` : ""}`,
      );
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function refreshNode(nodeId) {
    try {
      await api(`/api/nodes/${nodeId}/refresh`, { method: "POST", body: {} });
      showToast("节点指标已刷新");
      await loadDashboard(true);
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  async function deleteNode(nodeId) {
    const node = findNode(nodeId);
    if (!node) return;
    confirmDialog(
      "删除节点",
      `确认从平台移除“${node.name}”吗？远端机器不会发生任何变更。`,
      "删除节点",
      async () => {
        try {
          await api(`/api/nodes/${nodeId}`, { method: "DELETE" });
          state.selectedNodes.delete(Number(nodeId));
          if (state.drawerNodeId === Number(nodeId)) closeDrawer();
          closeModal();
          showToast("节点已移除");
          await loadDashboard();
        } catch (error) {
          showToast(error.message, "error");
        }
      },
      true,
    );
  }

  async function runBatchAction(action) {
    const ids = Array.from(state.selectedNodes);
    if (!ids.length) return;
    if (["group", "tags"].includes(action)) {
      openBatchTextModal(action);
      return;
    }
    if (action === "delete") {
      confirmDialog(
        "批量删除节点",
        `确认移除选中的 ${ids.length} 个节点吗？远端机器不会发生任何变更。`,
        "批量删除",
        async () => {
          await executeBatch(action, {});
        },
        true,
      );
      return;
    }
    await executeBatch(action, {});
  }

  async function executeBatch(action, extra) {
    try {
      const ids = Array.from(state.selectedNodes);
      const result = await api("/api/nodes/batch", {
        method: "POST",
        body: { ids, action, ...extra },
      });
      state.selectedNodes.clear();
      closeModal();
      showToast(`已处理 ${result.affected} 个节点`);
      await loadDashboard();
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  function bindEvents() {
    document.addEventListener("click", async (event) => {
      const nav = event.target.closest("[data-view]");
      if (nav) {
        navigate(nav.dataset.view);
        return;
      }
      const actionElement = event.target.closest("[data-action]");
      const action = actionElement?.dataset.action;
      if (action) {
        if (action === "reload-page") window.location.reload();
        else if (action === "toggle-password") {
          const input = $("#authPassword");
          input.type = input.type === "password" ? "text" : "password";
          actionElement.innerHTML = icon(input.type === "password" ? "eye" : "eye-off");
          refreshIcons(actionElement);
        } else if (action === "toggle-menu") {
          $(".sidebar")?.classList.toggle("open");
        } else if (action === "logout") {
          await logout();
        } else if (action === "refresh-all") {
          const button = actionElement;
          setButtonBusy(button, true, "刷新");
          try {
            await loadDashboard(true);
            showToast("集群数据已刷新");
          } finally {
            setButtonBusy(button, false);
          }
        } else if (action === "open-bulk-add") {
          openBulkAddModal();
        } else if (action === "scan-network" || action === "scan-network-inline") {
          await openScanModal();
        } else if (action === "refresh-discovery") {
          await loadDiscovery();
        } else if (action === "import-selected") {
          await importSelectedDiscovered();
        } else if (action === "close-modal") {
          closeModal();
        } else if (action === "modal-backdrop") {
          if (event.target === actionElement) closeModal();
        } else if (action === "confirm-modal") {
          const confirmAction = state.confirmAction;
          closeModal();
          if (confirmAction) await confirmAction();
        } else if (action === "close-drawer") {
          closeDrawer();
        } else if (action === "refresh-node") {
          await refreshNode(Number(actionElement.dataset.id));
        } else if (action === "test-node") {
          await testNodeConnection(
            Number(actionElement.dataset.id),
            actionElement,
          );
        } else if (action === "edit-node") {
          openEditNodeModal(Number(actionElement.dataset.id));
        } else if (action === "delete-node") {
          await deleteNode(Number(actionElement.dataset.id));
        } else if (action === "history-range") {
          changeHistoryRange(
            Number(actionElement.dataset.id),
            Number(actionElement.dataset.minutes),
          );
        } else if (action === "clear-selection") {
          state.selectedNodes.clear();
          renderNodes();
          refreshIcons($("#content"));
        } else if (action === "export-audit") {
          exportAudit();
        } else if (action === "model-refresh") {
          const button = actionElement;
          setButtonBusy(button, true, "刷新");
          try {
            await Promise.all([loadModelOverview(true), loadModelLogs(true)]);
          } finally {
            setButtonBusy(button, false);
          }
        } else if (action === "model-log-refresh") {
          await loadModelLogs();
        } else if (action === "model-log-download") {
          downloadModelLogs();
        } else if (action === "model-service-start") {
          askModelServiceAction("start", actionElement.dataset.unit);
        } else if (action === "model-service-stop") {
          askModelServiceAction("stop", actionElement.dataset.unit);
        } else if (action === "model-service-switch") {
          askModelServiceAction("switch", actionElement.dataset.unit);
        } else if (action === "open-help") {
          openHelp(actionElement.dataset.help);
        } else if (action === "chat-send") {
          await sendChat();
        } else if (action === "chat-reset") {
          resetChat();
        } else if (action === "alert-rules-save") {
          await saveAlertRules();
        } else if (action === "alert-rules-reset") {
          resetAlertRules();
        }
        return;
      }
      const bulkButton = event.target.closest("[data-bulk-action]");
      if (bulkButton) {
        await runBatchAction(bulkButton.dataset.bulkAction);
        return;
      }
      const nodeCard = event.target.closest(".node-card[data-node-id]");
      if (nodeCard) {
        openDrawer(Number(nodeCard.dataset.nodeId));
        return;
      }
      const alertNode = event.target.closest("button[data-node-id]");
      if (alertNode) {
        openDrawer(Number(alertNode.dataset.nodeId));
      }
    });

    document.addEventListener("change", (event) => {
      const target = event.target;
      if (target.matches("[data-select-node]")) {
        const id = Number(target.dataset.selectNode);
        if (target.checked) state.selectedNodes.add(id);
        else state.selectedNodes.delete(id);
        renderNodes();
        refreshIcons($("#content"));
      } else if (target.matches("[data-discovery-host]")) {
        const host = target.dataset.discoveryHost;
        if (target.checked) state.selectedDiscovery.add(host);
        else state.selectedDiscovery.delete(host);
        renderDiscovery();
        refreshIcons($("#content"));
      } else if (target.id === "selectAllNodes") {
        const nodes = filteredNodes();
        if (target.checked) nodes.forEach((node) => state.selectedNodes.add(node.id));
        else nodes.forEach((node) => state.selectedNodes.delete(node.id));
        renderNodes();
        refreshIcons($("#content"));
      } else if (target.id === "groupFilter") {
        state.filters.group = target.value;
        renderNodes();
        refreshIcons($("#content"));
      } else if (target.id === "statusFilter") {
        state.filters.status = target.value;
        renderNodes();
        refreshIcons($("#content"));
      } else if (target.id === "modelLogRank") {
        state.modelRank = target.value;
        state.modelLogs = [];
        loadModelLogs();
      } else if (target.id === "scanInterface") {
        state.scanInterface = target.value;
      }
    });

    document.addEventListener("input", (event) => {
      if (event.target.id === "nodeSearch") {
        state.filters.search = event.target.value;
        const position = event.target.selectionStart;
        renderNodes();
        refreshIcons($("#content"));
        const input = $("#nodeSearch");
        if (input) {
          input.focus();
          input.setSelectionRange(position, position);
        }
      } else if (event.target.id === "modelLogSearch") {
        state.modelLogSearch = event.target.value;
        const position = event.target.selectionStart;
        renderModelLogs();
        const input = $("#modelLogSearch");
        if (input) {
          input.focus();
          input.setSelectionRange(position, position);
        }
      }
    });

    document.addEventListener("submit", async (event) => {
      const form = event.target;
      event.preventDefault();
      if (form.id === "authForm") await submitAuth(form);
      else if (form.id === "bulkAddForm") await submitBulkAdd(form);
      else if (form.id === "editNodeForm") await submitEditNode(form);
      else if (form.id === "scanForm") {
        const data = new FormData(form);
        await startScan(
          String(data.get("interface") || ""),
          String(data.get("scanCidrs") || ""),
          readSshCredentials(data),
        );
      } else if (form.id === "scanModalForm") {
        const data = new FormData(form);
        await startScan(
          String(data.get("interface") || ""),
          String(data.get("cidrs") || ""),
          readSshCredentials(data),
        );
      } else if (form.id === "batchTextForm") {
        const kind = form.dataset.kind;
        const raw = new FormData(form).get("value") || "";
        const extra = kind === "group" ? { group_name: raw } : { tags: raw };
        await executeBatch(kind, extra);
      } else if (form.id === "settingsForm") await saveSettings(form);
    });

    document.addEventListener("keydown", (event) => {
      if (
        event.target.id === "chatInput" &&
        event.key === "Enter" &&
        !event.shiftKey &&
        !event.isComposing
      ) {
        event.preventDefault();
        sendChat();
        return;
      }
      const nodeCard = event.target.closest(".node-card[data-node-id]");
      if (nodeCard && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        openDrawer(Number(nodeCard.dataset.nodeId));
        return;
      }
      if (event.key === "Escape") {
        if ($("#modalRoot").innerHTML) closeModal();
        else if (state.drawerNodeId) closeDrawer();
      }
    });

    window.addEventListener("resize", scheduleResponsiveRedraw, { passive: true });
    if ("ResizeObserver" in window) {
      const contentObserver = new ResizeObserver(scheduleResponsiveRedraw);
      contentObserver.observe($("#content"));
    }
  }

  async function submitAuth(form) {
    const button = $("button[type=submit]", form);
    const errorBox = $("#authError");
    const data = new FormData(form);
    const username = String(data.get("username") || "").trim();
    const password = String(data.get("password") || "");
    if (state.authMode === "setup" && password !== String(data.get("password_confirm") || "")) {
      errorBox.textContent = "两次输入的密码不一致";
      errorBox.hidden = false;
      return;
    }
    setButtonBusy(button, true, state.authMode === "setup" ? "创建中" : "登录中");
    errorBox.hidden = true;
    try {
      const payload = await api(state.authMode === "setup" ? "/api/setup" : "/api/login", {
        method: "POST",
        body: { username, password },
      });
      state.user = payload.user;
      state.csrf = payload.csrf_token;
      form.reset();
      showApp();
      await Promise.all([loadSettings(true), loadDashboard()]);
      startPolling();
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function logout() {
    try {
      await api("/api/logout", { method: "POST", body: {} });
    } catch (error) {
      if (error.status !== 401) showToast(error.message, "error");
    }
    state.user = null;
    state.csrf = null;
    state.dashboard = null;
    state.discovery = null;
    state.modelOverview = null;
    state.modelLogs = [];
    state.modelLogsUpdatedAt = 0;
    state.clusterHistory = null;
    state.clusterHistoryLoadedAt = 0;
    state.selectedNodes.clear();
    showAuth("login");
  }

  async function submitBulkAdd(form) {
    const button = $("button[type=submit]", form);
    const errorBox = $("#modalError");
    const data = new FormData(form);
    setButtonBusy(button, true, "纳管中");
    errorBox.hidden = true;
    try {
      const result = await api("/api/nodes/bulk", {
        method: "POST",
        body: {
          input: data.get("input"),
          group_name: data.get("group_name"),
          tags: data.get("tags"),
          ...readSshCredentials(data),
        },
      });
      closeModal();
      const skipped = result.skipped?.length || 0;
      showToast(`已添加 ${result.added} 个节点${skipped ? `，跳过 ${skipped} 个` : ""}`);
      navigate("nodes");
      await loadDashboard();
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function submitEditNode(form) {
    const button = $("button[type=submit]", form);
    const errorBox = $("#modalError");
    const data = new FormData(form);
    const password = String(data.get("ssh_password") || "");
    const clearPassword = Boolean(data.get("clear_ssh_password"));
    if (password && clearPassword) {
      errorBox.textContent = "不能同时设置新密码和清除已保存密码";
      errorBox.hidden = false;
      return;
    }
    setButtonBusy(button, true, "保存中");
    errorBox.hidden = true;
    try {
      await api(`/api/nodes/${form.dataset.id}`, {
        method: "PATCH",
        body: {
          name: data.get("name"),
          host: data.get("host"),
          group_name: data.get("group_name"),
          tags: data.get("tags"),
          ssh_port: data.get("ssh_port") ? Number(data.get("ssh_port")) : null,
          ssh_user: data.get("ssh_user"),
          ssh_key_path: data.get("ssh_key_path"),
          ssh_password: password,
          clear_ssh_password: clearPassword,
        },
      });
      closeModal();
      showToast("节点配置已保存");
      await loadDashboard();
    } catch (error) {
      errorBox.textContent = error.message;
      errorBox.hidden = false;
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function saveSettings(form) {
    const button = $("button[type='submit'][form='settingsForm']");
    const data = {};
    for (const field of state.settingsFields) {
      const input = form.elements.namedItem(field.key);
      if (!input) continue;
      if (field.type === "bool") {
        data[field.key] = input.checked ? 1 : 0;
      } else if (field.type === "int" || field.type === "float") {
        data[field.key] = Number(input.value);
      } else {
        data[field.key] = String(input.value ?? "");
      }
    }
    if (!form.reportValidity()) return;
    setButtonBusy(button, true, "保存中");
    try {
      const result = await api("/api/settings", { method: "PATCH", body: data });
      applySettingsPayload(result);
      state.clusterHistory = null;
      state.clusterHistoryLoadedAt = 0;
      await Promise.all([
        loadClusterHistory(state.historyRange),
        loadDashboard(true),
      ]);
      startPolling();
      setButtonBusy(button, false);
      renderCurrentView();
      showToast("系统设置已保存");
    } catch (error) {
      showToast(error.message, "error");
      setButtonBusy(button, false);
    }
  }

  async function importSelectedDiscovered() {
    const hosts = Array.from(state.selectedDiscovery);
    if (!hosts.length) return;
    try {
      const result = await api("/api/discovery/import", {
        method: "POST",
        body: { hosts },
      });
      state.selectedDiscovery.clear();
      showToast(`已纳管 ${result.added} 个节点`);
      await Promise.all([loadDiscovery(true), loadDashboard(true)]);
    } catch (error) {
      showToast(error.message, "error");
    }
  }

  function filteredNodes() {
    const nodes = state.dashboard?.nodes || [];
    return nodes.filter((node) => {
      const haystack = [node.name, node.host, node.group_name, ...(node.tags || [])]
        .join(" ")
        .toLowerCase();
      return (
        (!state.filters.search || haystack.includes(state.filters.search.toLowerCase())) &&
        (!state.filters.group || node.group_name === state.filters.group) &&
        (!state.filters.status || node.status === state.filters.status)
      );
    });
  }

  function exportAudit() {
    if (!state.audit.length) return;
    const header = ["时间", "操作", "目标", "详情", "用户", "地址"];
    const rows = state.audit.map((log) => [
      formatDateTime(log.ts),
      log.action,
      log.target || "",
      log.detail || "",
      log.username || "system",
      log.remote_addr || "",
    ]);
    const csv = [header, ...rows]
      .map((row) => row.map((cell) => `"${String(cell ?? "").replaceAll('"', '""')}"`).join(","))
      .join("\n");
    const blob = new Blob([`\ufeff${csv}`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "spark-grid-audit.csv";
    link.click();
    URL.revokeObjectURL(url);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
