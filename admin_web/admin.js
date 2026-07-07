const state = {
  view: "dashboard",
  token: localStorage.getItem("rssaiToken") || "",
  currentDigest: null,
  principal: null,
};

function isOperator() {
  return state.principal && state.principal.kind === "operator";
}

async function loadPrincipal() {
  try {
    state.principal = await api("/api/auth/me");
  } catch (e) {
    state.principal = null;
  }
  return state.principal;
}

const initialParams = new URLSearchParams(location.search);
if (initialParams.get("token")) {
  state.token = initialParams.get("token").trim();
  localStorage.setItem("rssaiToken", state.token);
  initialParams.delete("token");
  const clean = `${location.pathname}${initialParams.toString() ? "?" + initialParams.toString() : ""}${location.hash}`;
  history.replaceState(null, "", clean);
}

const titles = {
  dashboard: ["总览", "PC 后端、本地数据库、App 心跳和 Tunnel 状态"],
  messages: ["RSS 消息", "查看摘要、手动刷新、打开详情和提问"],
  reading: ["PDF 阅读", "查看 PDF 总结、PDF 原文和问答"],
  feeds: ["RSS 源", "管理 OPML 中的 RSS 源"],
  settings: ["设置", "后端、AI、RSS、调度和 Tunnel 配置"],
  monitor: ["监控", "检查 App -> Tunnel -> Flask -> DB -> RSS/AI/PDF/Inbox 链路"],
  logs: ["日志", "后端 server.log 尾部"],
};

function h(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function cfgValue(obj, key, fallback) {
  return obj && obj[key] !== undefined && obj[key] !== null ? obj[key] : fallback;
}

const RSS_FETCH_FIELDS = [
  ["rss_min_interval_minutes", "最小抓取间隔(分)", 15, 1],
  ["rss_default_interval_minutes", "默认抓取间隔(分)", 60, 1],
  ["rss_unchanged_max_interval_minutes", "无更新最大间隔(分)", 360, 1],
  ["rss_max_interval_minutes", "最大抓取间隔(分)", 1440, 1],
  ["rss_feed_lease_minutes", "抓取租约时间(分)", 60, 1],
  ["rss_probe_cooldown_minutes", "单源探测冷却(分)", 60, 0],
  ["rss_host_workers", "Host 并发组数", 4, 1],
  ["rss_host_gap_min_seconds", "同Host休息最短(秒)", 5, 0],
  ["rss_host_gap_max_seconds", "同Host休息最长(秒)", 15, 0],
  ["rss_access_denied_cooldown_minutes", "403冷却基准(分)", 60, 1],
  ["rss_access_denied_max_cooldown_minutes", "403冷却上限(分)", 1440, 1],
  ["rss_rate_limited_base_cooldown_minutes", "429冷却基准(分)", 360, 1],
  ["rss_rate_limited_max_cooldown_minutes", "429冷却上限(分)", 10080, 1],
  ["rss_not_found_base_cooldown_minutes", "404冷却基准(分)", 1440, 1],
  ["rss_not_found_max_cooldown_minutes", "404冷却上限(分)", 10080, 1],
  ["rss_not_found_disable_failures", "404禁用失败次数", 3, 1],
  ["rss_client_error_base_cooldown_minutes", "4xx冷却基准(分)", 1440, 1],
  ["rss_client_error_max_cooldown_minutes", "4xx冷却上限(分)", 10080, 1],
  ["rss_client_error_disable_failures", "4xx禁用失败次数", 3, 1],
  ["rss_gone_cooldown_minutes", "410禁用冷却(分)", 10080, 1],
  ["rss_unsafe_tls_cooldown_minutes", "TLS/不安全URL冷却(分)", 10080, 1],
  ["rss_invalid_feed_base_cooldown_minutes", "无效Feed冷却基准(分)", 360, 1],
  ["rss_invalid_feed_max_cooldown_minutes", "无效Feed冷却上限(分)", 1440, 1],
  ["rss_transient_base_cooldown_minutes", "临时错误冷却基准(分)", 15, 1],
  ["rss_transient_max_cooldown_minutes", "临时错误冷却上限(分)", 360, 1],
  ["rss_wiley_403_min_cooldown_minutes", "Wiley 403冷却基准(分)", 1440, 1],
  ["rss_wiley_403_max_cooldown_minutes", "Wiley 403冷却上限(分)", 10080, 1],
].map(([key, label, defaultValue, min]) => ({key, label, defaultValue, min}));

function renderRssFetchFields(rss) {
  return RSS_FETCH_FIELDS.map(field => `
    <label>${h(field.label)}
      <input id="${h(field.key)}" type="number" min="${h(field.min)}" step="1" value="${h(cfgValue(rss, field.key, field.defaultValue))}">
    </label>`).join("");
}

function readRssFetchSettings() {
  const result = {};
  RSS_FETCH_FIELDS.forEach(field => {
    result[field.key] = Number(document.getElementById(field.key).value || field.defaultValue);
  });
  return result;
}

function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (!(options.body instanceof FormData)) headers["Content-Type"] = "application/json";
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  return fetch(path, Object.assign({}, options, {headers, credentials: "same-origin"})).then(async r => {
    if (r.status === 401) {
      document.getElementById("loginPanel").classList.remove("hidden");
      throw new Error("unauthorized");
    }
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    return r.json();
  });
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".nav").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("hidden", v.id !== view));
  document.getElementById("pageTitle").textContent = titles[view][0];
  document.getElementById("subtitle").textContent = titles[view][1];
  refresh();
}

function card(title, body) {
  return `<div class="card"><h2>${h(title)}</h2>${body}</div>`;
}

function collapsibleCard(key, title, body, options = {}) {
  const saved = sessionStorage.getItem(`collapse:${key}`);
  const isOpen = saved === null ? Boolean(options.open) : saved === "open";
  const meta = options.meta ? `<span class="collapsibleMeta">${h(options.meta)}</span>` : "";
  return `<details class="card collapsible" data-collapse-key="${h(key)}" ${isOpen ? "open" : ""}>
    <summary>
      <span class="collapsibleTitle">${h(title)}</span>
      ${meta}
    </summary>
    <div class="collapsibleBody">${body}</div>
  </details>`;
}

function copyField(label, value, placeholder = "未生成") {
  const display = value || placeholder;
  return `<label class="copyField">${h(label)}
    <div class="row">
      <input readonly value="${h(display)}">
      <button data-copy="${h(value || "")}" ${value ? "" : "disabled"}>复制</button>
    </div>
  </label>`;
}

function withToken(path) {
  if (!state.token) return path;
  const sep = path.includes("?") ? "&" : "?";
  return `${path}${sep}token=${encodeURIComponent(state.token)}`;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function cleanupSource(source) {
  const isRss = source === "rss";
  const label = isRss ? "RSS 消息" : "PDF 阅读";
  const resetText = isRss
    ? "同时清理最近 RSS 事件，并重置 RSS 去重节点和发布队列；下一次刷新会重新抓取当前条目。"
    : "同时清理 PDF 扫描节点和全部待匹配论文。";
  const keepText = isRss ? "订阅源配置不会删除。" : "原始 PDF 文件不会删除。";
  if (!window.confirm(`确定一键清理${label}吗？\n\n${resetText}\n${keepText}`)) return;

  const result = await api(`/api/admin/cleanup/${source}`, {method: "POST", body: "{}"});
  const freedMb = (Number(result.bytes_freed || 0) / 1024 / 1024).toFixed(2);
  const extra = isRss
    ? `，清理 ${result.events_deleted || 0} 条 RSS 事件`
    : `，清理 ${result.pending_papers_deleted || 0} 条待匹配论文`;
  window.alert(`${label}清理完成：删除 ${result.digests_deleted || 0} 条消息${extra}，释放 ${freedMb} MB。`);
  await refresh();
}

function splitDirs(value) {
  return String(value || "")
    .split(/[\n;]/)
    .map(v => v.trim())
    .filter(Boolean);
}

async function renderDashboard() {
  const o = await api("/api/admin/overview");
  const s = o.status || {};
  const app = o.app || {};
  const tunnel = o.tunnel || {};
  const server = o.server || {};
  const configuredUrl = tunnel.configured_url || "";
  const quickTunnelUrl = (tunnel.quick || {}).url || "";
  const tunnelUrl = tunnel.current_url || configuredUrl || quickTunnelUrl || "";
  const tunnelStatus = configuredUrl ? "固定服务器 URL 已启用" : ((tunnel.quick || {}).status || "");
  const authToken = server.auth_token || "";
  document.getElementById("dashboard").innerHTML = `
    ${card("App 连接信息", `
      <div class="grid compact">
        <label class="copyField">服务器 URL
          <div class="row">
            <input id="appServerUrl" value="${h(tunnelUrl)}" placeholder="https://your-server.example.com">
            <button data-copy="${h(tunnelUrl)}" ${tunnelUrl ? "" : "disabled"}>复制</button>
          </div>
        </label>
        ${copyField("服务器 Token", authToken, "未启用 Token")}
        ${copyField("Quick Tunnel URL（备用）", quickTunnelUrl, "Quick Tunnel 未连接")}
      </div>
      <div class="toolbar inlineToolbar">
        <button id="saveAppConnectionBtn" class="primary">保存服务器 URL</button>
        <button id="refreshTunnelBtn" class="primary">刷新 Quick Tunnel URL</button>
        <span id="tunnelRefreshStatus" class="label">${h(tunnelStatus)}</span>
      </div>
      <div class="label">已填写服务器 URL 时优先使用该地址；留空时使用当前 Quick Tunnel URL。</div>
    `)}
    <div class="grid">
      ${card("RSS 源", `<div class="stat">${s.feeds_count || 0}</div><div class="label">discovery ${s.rss_discovery_interval || "-"} 分钟 / publish ${s.rss_interval || "-"} 分钟</div>`)}
      ${card("摘要", `<div class="stat">${s.inbox_summaries || 0}</div><div class="label">RSS + PDF</div>`)}
      ${card("RSS 队列", `<div class="stat">${(s.rss_queue || {}).pending || 0}</div><div class="label">待发布</div>`)}
      ${card("App", `<div class="stat ${app.online ? "ok" : "warn"}">${app.online ? "在线" : "离线"}</div><div class="label">${h(app.last_seen || "无心跳")}</div>`)}
      ${card("Tunnel", `<div class="stat ${tunnel.process_running ? "ok" : "warn"}">${tunnel.process_running ? "运行" : "未运行"}</div><div class="label">${h(tunnelUrl || "未生成 URL")}</div>`)}
      ${card("Flask", `<div class="stat ${server.listening ? "ok" : "bad"}">${server.listening ? "监听中" : "未监听"}</div><div class="label">${h(server.local_url || "")}</div>`)}
      ${card("后台宿主", `<div class="runtimeName">${h(server.program_name || "SciToday_admin")}</div><div class="label ${server.admin_process_running ? "ok" : "warn"}">${server.admin_process_running ? "托盘运行中" : "未检测到托盘程序"}</div>`)}
    </div>
    ${card("任务进度", `<pre>${h(JSON.stringify(o.progress || {}, null, 2))}</pre>`)}
    ${card("最近事件", renderEvents(o.recent_events || []))}
  `;
}

function renderEvents(events) {
  if (!events.length) return `<p>暂无事件</p>`;
  return `<div class="list">${events.map(e => `<div class="item">
    <span class="badge ${e.level === "error" ? "bad" : e.level === "warning" ? "warn" : "ok"}">${h(e.level)}</span>
    <span class="meta">${h(e.time)} · ${h(e.type)}</span>
    <div class="itemTitle">${h(e.message)}</div>
  </div>`).join("")}</div>`;
}

function statusClass(status) {
  if (status === "published" || status === "processed" || status === "ok") return "ok";
  if (status === "pending" || status === "unmatched" || status === "too_little_text") return "warn";
  return status ? "bad" : "";
}

function renderProgress(progress, keys) {
  const cards = keys.map(k => {
    const p = (progress || {})[k] || {};
    const total = Number(p.total || 0);
    const current = Number(p.current || 0);
    const count = total ? `${current}/${total}` : (p.active ? "运行中" : "空闲");
    return card(k, `<div class="stat ${p.active ? "warn" : "ok"}">${h(count)}</div><div class="label">${h(p.message || (p.active ? "运行中" : "空闲"))}</div>`);
  }).join("");
  return `<div class="grid">${cards}</div>`;
}

function renderRssQueue(queue) {
  const stats = queue.stats || {};
  const rows = (queue.items || []).map(item => {
    const digest = item.digest || {};
    const authors = Array.isArray(item.authors) ? item.authors.join(", ") : (item.authors || "");
    const aiTitle = digest.cn_title || digest.title || "";
    const digestButton = digest.filename
      ? `<button data-open="${h(digest.filename)}" data-title="${h(aiTitle || item.title || "AI 返回内容")}">打开 AI 返回内容</button>`
      : `<span class="label">暂无可打开的 AI 返回内容</span>`;
    return `<div class="queuePair">
      <div class="queueHeader">
        <div>
          <span class="badge ${statusClass(item.status)}">${h(item.status)}</span>
          <span class="meta">入队 ${h(item.created || "-")} · 发布 ${h(item.published || "-")}</span>
        </div>
        <div class="queueActions">
          ${item.link ? `<a class="badge" href="${h(item.link)}" target="_blank">原文链接</a>` : ""}
          ${digestButton}
        </div>
      </div>
      <div class="queueColumns">
        <section class="queuePane">
          <h3>RSS 推送（英文）</h3>
          <div class="itemTitle">${h(item.title || "无标题")}</div>
          <div class="meta">${h(item.feed || "")}${item.doi ? " · DOI " + h(item.doi) : ""}</div>
          <div class="meta">${h(item.article_type || "")}${item.first_author ? " · 一作 " + h(item.first_author) : ""}${authors ? " · 作者 " + h(authors) : ""}</div>
          <p>${h(item.summary || "RSS 未提供摘要。")}</p>
        </section>
        <section class="queuePane aiPane">
          <h3>AI 返回内容（中文）</h3>
          ${aiTitle ? `<div class="itemTitle">${h(aiTitle)}</div>` : `<div class="itemTitle mutedTitle">尚未匹配到 AI 返回内容</div>`}
          <div class="meta">${digest.keywords ? "关键词：" + h(digest.keywords) : "关键词：未生成"}${digest.timestamp ? " · " + h(digest.timestamp) : ""}</div>
          <p>${h(digest.preview || (item.status === "published" ? "已发布，但未在摘要索引中匹配到中文内容。" : "等待发布后生成中文 AI 返回内容。"))}</p>
          ${item.error ? `<div class="queueError">${h(item.error)}</div>` : ""}
        </section>
      </div>
    </div>`;
  }).join("");
  return `
    <div class="grid">
      ${card("待发布", `<div class="stat">${stats.pending || 0}</div><div class="label">pending</div>`)}
      ${card("已发布", `<div class="stat">${stats.published || 0}</div><div class="label">published</div>`)}
      ${card("失败", `<div class="stat ${stats.error ? "bad" : ""}">${stats.error || 0}</div><div class="label">error</div>`)}
    </div>
    ${collapsibleCard(
      "rss-queue-details",
      "RSS 队列明细",
      `<div class="queueList">${rows || `<div class="panel">暂无队列记录</div>`}</div>`,
      {meta: `${(queue.items || []).length} 条`}
    )}
  `;
}

function renderPdfQueue(queue) {
  const stats = queue.stats || {};
  const seen = stats.pdf_seen || {};
  const pendingRows = (queue.pending || []).map(p => `<tr>
    <td><div class="itemTitle">${h(p.title || "无标题")}</div><div class="meta">${h(p.feed)} ${p.doi ? " · DOI " + h(p.doi) : ""}</div></td>
    <td>${h(p.first_author)}</td>
    <td>${h(p.created)}</td>
    <td>${p.link ? `<a href="${h(p.link)}" target="_blank">打开</a>` : ""}</td>
  </tr>`).join("");
  const recentRows = (queue.recent || []).map(p => `<tr>
    <td><div class="itemTitle">${h(p.filename || p.path)}</div><div class="meta">${h(p.matched_title)}</div></td>
    <td><span class="badge ${statusClass(p.status)}">${h(p.status)}</span></td>
    <td>${h(p.time)}</td>
    <td>${h(p.path)}</td>
  </tr>`).join("");
  const dirRows = (queue.download_dirs || []).map(d => `<tr>
    <td>${h(d.path)}</td>
    <td class="${d.exists ? "ok" : "warn"}">${d.exists ? "存在" : "不存在"}</td>
    <td>${h(d.pdf_count)}</td>
  </tr>`).join("");
  return `
    <div class="grid">
      ${card("待匹配论文", `<div class="stat">${stats.pending_total || 0}</div><div class="label">pending_papers</div>`)}
      ${card("扫描 PDF", `<div class="stat">${stats.pdf_files || 0}</div><div class="label">下载目录 + 上传目录</div>`)}
      ${card("PDF 记录", `<div class="stat">${seen.total || 0}</div><div class="label">processed ${seen.processed || 0} / error ${seen.error || 0}</div>`)}
    </div>
    ${collapsibleCard(
      "pdf-pending-papers",
      "待匹配论文",
      `<div class="tableWrap"><table><thead><tr><th>论文</th><th>一作</th><th>入库</th><th>链接</th></tr></thead><tbody>${pendingRows || `<tr><td colspan="4">暂无待匹配论文</td></tr>`}</tbody></table></div>`,
      {meta: `${stats.pending_total || 0} 条`}
    )}
    ${collapsibleCard(
      "pdf-recent-processing",
      "最近 PDF 处理",
      `<div class="tableWrap"><table><thead><tr><th>PDF</th><th>状态</th><th>时间</th><th>路径</th></tr></thead><tbody>${recentRows || `<tr><td colspan="4">暂无 PDF 处理记录</td></tr>`}</tbody></table></div>`,
      {meta: `${(queue.recent || []).length} 条`}
    )}
    ${collapsibleCard(
      "pdf-scan-directories",
      "PDF 扫描目录",
      `<div class="tableWrap"><table><thead><tr><th>路径</th><th>状态</th><th>PDF 数</th></tr></thead><tbody>${dirRows || `<tr><td colspan="3">暂无目录</td></tr>`}</tbody></table></div>`,
      {meta: `${(queue.download_dirs || []).length} 个`}
    )}
  `;
}

async function renderDigests(target, source) {
  const list = await api(`/api/digests?limit=100&source=${encodeURIComponent(source)}`);
  const [overview, queue, events] = await Promise.all([
    api("/api/admin/overview"),
    source === "rss" ? api("/api/admin/rss-queue?limit=100") : api("/api/admin/pdf-queue?limit=100"),
    source === "rss" ? api("/api/admin/events?limit=30&source=rss") : Promise.resolve([]),
  ]);
  const progressKeys = source === "rss" ? ["rss", "rss_discovery", "rss_publish"] : ["pdf"];
  const queueHtml = source === "rss" ? renderRssQueue(queue) : renderPdfQueue(queue);
  document.getElementById(target).innerHTML = `
    <div class="toolbar">
      <button class="primary" data-action="${source === "rss" ? "runRss" : "runPdf"}">${source === "rss" ? "立即RSS刷新" : "立即PDF刷新"}</button>
      ${source === "rss" ? `<button data-action="discovery">只发现入队</button><button data-action="publish">只发布队列</button>` : ""}
      <button class="danger" data-action="${source === "rss" ? "cleanupRss" : "cleanupPdf"}">${source === "rss" ? "一键清理 RSS" : "一键清理 PDF 阅读"}</button>
    </div>
    ${renderProgress(overview.progress || {}, progressKeys)}
    ${queueHtml}
    <div class="list">${list.map(d => `<div class="item">
      <div class="itemTitle">${h(d.cn_title || d.title)}</div>
      <div class="meta">${h(d.timestamp)} · ${h(d.journal)} · ${h(d.keywords)}</div>
      <p>${h(d.preview)}</p>
      <button data-open="${h(d.filename)}" data-title="${h(d.title)}">打开</button>
      ${source === "pdf" ? `<a class="badge" href="/api/pdf?filename=${encodeURIComponent(d.filename)}" target="_blank">PDF原文</a>` : ""}
    </div>`).join("") || `<div class="panel">暂无消息</div>`}</div>`;
  if (source === "rss") {
    document.getElementById(target).innerHTML += collapsibleCard(
      "rss-recent-events",
      "最近 RSS 事件",
      renderEvents(events),
      {meta: `${events.length} 条`}
    );
  }
}

async function renderFeeds() {
  const [feeds, cfg] = await Promise.all([api("/api/feeds"), api("/api/admin/settings")]);
  const opmlPath = ((cfg.rss || {}).opml_path) || "";
  document.getElementById("feeds").innerHTML = `
    ${card("RSS 源同步", `
      <div class="grid">
        <label>当前 OPML<input readonly value="${h(opmlPath)}"></label>
        <label>源数量<input readonly value="${h(feeds.length)}"></label>
      </div>
      <div class="row importRow">
        <input id="opmlImportFile" type="file" accept=".opml,.xml,text/xml">
        <button id="importOpmlBtn">导入 OPML</button>
      </div>
    `)}
    <div class="panel">
      <h2>添加 RSS 源</h2>
      <div class="row"><input id="feedTitle" placeholder="名称"><input id="feedUrl" placeholder="RSS URL"><button id="addFeedBtn">添加</button></div>
    </div>
    ${collapsibleCard(
      "rss-source-list",
      "RSS 源名称",
      `<div class="tableWrap"><table><thead><tr><th>名称</th><th>URL</th><th></th></tr></thead><tbody>
        ${feeds.map((f, i) => `<tr>
          <td><input id="feedTitleEdit${i}" value="${h(f.title)}"></td>
          <td><input id="feedUrlEdit${i}" value="${h(f.url)}"></td>
          <td class="nowrap">
            <button data-update-feed="${h(encodeURIComponent(f.url))}" data-feed-index="${i}">保存</button>
            <button class="danger" data-delete-feed="${h(encodeURIComponent(f.url))}">删除</button>
          </td>
        </tr>`).join("") || `<tr><td colspan="3">暂无 RSS 源</td></tr>`}
      </tbody></table></div>`,
      {meta: `${feeds.length} 个`}
    )}`;
}

function renderIdentityPanel() {
  const p = state.principal || {};
  const scopes = (p.scopes || []).map(s => `<span class="badge">${h(s)}</span>`).join(" ") || "无";
  const kindLabel = {operator: "运营者", tenant: "租户", developer: "开发模式"}[p.kind] || (p.kind || "未知");
  return `<div class="panel">
    <h2>当前身份</h2>
    <div class="grid">
      <label>当前租户<input readonly value="${h(p.tenant_id || "未知")}"></label>
      <label>身份类型<input readonly value="${h(kindLabel)}"></label>
      <label class="copyField">权限 Scope<div class="scopeList">${scopes}</div></label>
    </div>
  </div>`;
}

const TENANT_STATUS_LABEL = {
  active: "启用",
  provisioning: "初始化中",
  suspended: "已停用",
  deleted: "已删除",
};

async function renderTenantPanel() {
  const data = await api("/api/admin/tenants");
  const tenants = data.tenants || [];
  const rows = tenants.map(t => {
    const statusCls = t.status === "active" ? "ok" : t.status === "deleted" ? "bad" : "warn";
    const tokenInfo = t.token_count === null ? "-"
      : `${t.active_token_count}/${t.token_count}`;
    let actions = "";
    if (t.is_owner) {
      actions = `<span class="label">系统租户</span>`;
    } else if (t.status === "deleted") {
      actions = `<button class="danger" data-purge-tenant="${h(t.id)}" data-tenant-name="${h(t.display_name)}">备份并彻底删除</button>`;
    } else {
      actions = `<button data-add-token="${h(t.id)}" data-tenant-name="${h(t.display_name)}">新增 Token</button>
        <button class="danger" data-delete-tenant="${h(t.id)}" data-tenant-name="${h(t.display_name)}">删除</button>`;
    }
    return `<tr>
      <td><div class="itemTitle">${h(t.display_name)}</div><div class="meta">${h(t.id)}</div></td>
      <td class="${statusCls}">${h(TENANT_STATUS_LABEL[t.status] || t.status)}</td>
      <td>${h(t.created_at)}</td>
      <td>${h(tokenInfo)}</td>
      <td>${actions}</td>
    </tr>`;
  }).join("");
  return `<div class="panel">
    <h2>租户管理</h2>
    <div class="tableWrap"><table>
      <thead><tr><th>租户</th><th>状态</th><th>创建时间</th><th>Token(启用/全部)</th><th>操作</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="5">暂无租户</td></tr>`}</tbody>
    </table></div>
    <h3 class="subhead">添加租户</h3>
    <div class="row">
      <input id="newTenantName" placeholder="显示名称">
      <button id="addTenantBtn" class="primary">创建并生成 Token</button>
    </div>
    <div class="scopeChoices">
      <label class="checkRow"><input type="checkbox" class="tenantScope" value="app" checked>app（读取/推送/上传）</label>
      <label class="checkRow"><input type="checkbox" class="tenantScope" value="ai_config_write">ai_config_write（改 AI 连接/提示词）</label>
      <label class="checkRow"><input type="checkbox" class="tenantScope" value="tenant_admin">tenant_admin（清理/改配置）</label>
    </div>
    <div class="label">创建后 Token 仅显示一次；默认仅授予 app 权限。</div>
  </div>`;
}

function selectedTenantScopes() {
  return Array.from(document.querySelectorAll(".tenantScope:checked")).map(el => el.value);
}

function showTokenDialog(title, token, meta) {
  document.getElementById("tokenDialogTitle").textContent = title;
  const scopes = (meta && meta.scopes ? meta.scopes : []).map(s => `<span class="badge">${h(s)}</span>`).join(" ");
  document.getElementById("tokenDialogBody").innerHTML = `
    ${copyField("Token", token, "")}
    <div class="grid">
      <label>Token ID<input readonly value="${h(meta && meta.id || "")}"></label>
      <label class="copyField">Scope<div class="scopeList">${scopes || "无"}</div></label>
    </div>`;
  document.getElementById("tokenDialog").showModal();
}

async function renderSettings() {
  const [cfg, local] = await Promise.all([
    api("/api/admin/settings"),
    api("/api/admin/local-settings"),
  ]);
  const identityHtml = renderIdentityPanel();
  let tenantHtml = "";
  if (isOperator()) {
    try {
      tenantHtml = await renderTenantPanel();
    } catch (err) {
      // 租户面板失败不应连累整个设置页；降级为提示。
      tenantHtml = `<div class="panel"><h2>租户管理</h2><div class="label bad">加载租户列表失败：${h(err.message)}</div></div>`;
    }
  }
  const pc = cfg.pc || {};
  const server = cfg.server || {};
  const rss = cfg.rss || {};
  const preferenceWeights = rss.preference_weights || {
    pdf_matched: 100,
    interested: 40,
    is_read: 10,
    disliked: -70,
  };
  const ai = cfg.ai || {};
  const tray = (local || {}).tray || {};
  const startup = (local || {}).startup || {};
  const quickTunnel = pc.quick_tunnel || {};
  const configuredUrl = pc.cloudflare_tunnel_url || "";
  const currentUrl = pc.current_tunnel_url || configuredUrl || quickTunnel.url || "";
  const authToken = server.auth_token || "";
  const localHost = tray.host || server.effective_host || server.host || "127.0.0.1";
  const localPort = tray.port || server.effective_port || server.port || 5200;
  const localDataDir = tray.data_dir || pc.data_dir || "";
  const localDownloadDirs = (tray.download_dirs || pc.download_dirs || []).join("\n");
  document.getElementById("settings").innerHTML = `
    ${identityHtml}
    ${tenantHtml}
    <div class="panel">
      <h2>调度</h2>
      <div class="grid">
        <label>RSS discovery 间隔(分)<input id="rssDiscoveryInterval" type="number" min="15" value="${h((cfg.schedule || {}).rss_discovery_interval_minutes || 60)}"></label>
        <label>RSS publish 间隔(分)<input id="rssInterval" type="number" value="${h((cfg.schedule || {}).rss_interval_minutes || 30)}"></label>
        <label>PDF 间隔(分)<input id="pdfInterval" type="number" value="${h((cfg.schedule || {}).pdf_interval_minutes || 5)}"></label>
        <label>启用<select id="scheduleEnabled"><option value="true">true</option><option value="false">false</option></select></label>
      </div>
    </div>
    <div class="panel">
      <h2>RSS</h2>
      <div class="grid">
        <label>OPML 路径<input id="rssOpmlPath" value="${h(rss.opml_path || "")}"></label>
        <label>每源抓取上限<input id="perFeedLimit" type="number" min="1" value="${h(rss.per_feed_limit || 3)}"></label>
        <label>每轮发布上限<input id="maxPushItems" type="number" min="1" value="${h(rss.max_push_items || 20)}"></label>
        <label>RSS 抓取最近天数<input id="rssLookbackDays" type="number" min="1" max="365" value="${h(rss.lookback_days || 7)}"></label>
        <label>最近重置时间<input readonly value="${h(rss.last_reset_at || "尚未手动重置")}"></label>
        <label>当前抓取起点<input readonly value="${h(rss.fetch_since_at || "")}"></label>
      </div>
      <div class="toolbar inlineToolbar">
        <button id="resetRssTimeBtn">按当前天数重置抓取起点</button>
        <span class="label">默认仅抓取最近 7 天；无发布日期的条目仍按源内最新条目处理。</span>
      </div>
    </div>
    <div class="panel">
      <h2>RSS 抓取策略</h2>
      <div class="grid">
        ${renderRssFetchFields(rss)}
      </div>
    </div>
    <div class="panel">
      <h2>个性化推荐权重</h2>
      <div class="grid">
        <label>PDF 匹配<input id="pdfMatchedWeight" type="number" min="0" max="100" step="1" value="${h(preferenceWeights.pdf_matched ?? 100)}"></label>
        <label>感兴趣<input id="interestedWeight" type="number" min="0" max="100" step="1" value="${h(preferenceWeights.interested ?? 40)}"></label>
        <label>已读<input id="readWeight" type="number" min="0" max="100" step="1" value="${h(preferenceWeights.is_read ?? 10)}"></label>
        <label>不喜欢<input id="dislikedWeight" type="number" min="-100" max="0" step="1" value="${h(preferenceWeights.disliked ?? -70)}"></label>
      </div>
      <div class="label">正向权重范围 0–100，不喜欢范围 -100–0，并保持 PDF匹配 ≥ 感兴趣 ≥ 已读 ≥ 0 ≥ 不喜欢。显式“不喜欢”优先于PDF匹配和已读；保存后将重算个人偏好数据库并更新兴趣画像。</div>
    </div>
    <div class="panel">
      <h2>AI</h2>
      <div class="grid">
        <label>Base URL<input id="aiBaseUrl" value="${h(ai.base_url || "")}"></label>
        <label>Model<input id="aiModel" value="${h(ai.model || "")}"></label>
        <label>API Key<input id="aiKey" type="password" value="${h(ai.api_key || "")}"></label>
      </div>
      <label>System Prompt<textarea id="systemPrompt" rows="3">${h(ai.system_prompt || "")}</textarea></label>
      <label>RSS Prompt<textarea id="rssPrompt" rows="5">${h(ai.rss_prompt || "")}</textarea></label>
      <label>PDF Prompt<textarea id="pdfPrompt" rows="5">${h(ai.pdf_prompt || "")}</textarea></label>
    </div>
    <div class="panel">
      <h2>Server</h2>
      <div class="grid">
        <label>配置 Host<input id="serverHost" value="${h(localHost)}"></label>
        <label>配置 Port<input id="serverPort" type="number" value="${h(localPort)}"></label>
        <label>Effective URL<input readonly value="${h(server.effective_local_url || "")}"></label>
        <label>服务器 URL<input id="serverPublicUrl" value="${h(configuredUrl)}" placeholder="https://your-server.example.com"></label>
        <label>服务器 Token<input id="serverAuthToken" type="password" value="${h(authToken)}"></label>
      </div>
    </div>
    <div class="panel">
      <h2>本地后台</h2>
      <div class="grid">
        <label>后台程序<input readonly value="${h(local.program_name || "SciToday_admin")}"></label>
        <label>运行状态<input readonly value="${local.process_running ? "托盘运行中" : "未检测到托盘程序"}"></label>
        <label>程序路径<input readonly value="${h(local.executable_path || "")}"></label>
        <label class="checkRow"><input id="startupEnabled" type="checkbox" ${startup.enabled ? "checked" : ""}>开机自启</label>
        <label>安装目录<input readonly value="${h(local.install_dir || pc.install_dir || "")}"></label>
        <label>数据目录<input id="localDataDir" value="${h(localDataDir)}"></label>
        <label>服务器数据根<input id="serverDataDir" value="${h(local.server_data_dir || "")}"></label>
        <label class="checkRow"><input id="tunnelEnabled" type="checkbox" ${tray.tunnel_enabled !== false ? "checked" : ""}>由后台宿主管理 Tunnel</label>
        <label>托盘配置<input readonly value="${h(local.tray_config_path || "")}"></label>
        <label>命令文件<input readonly value="${h(local.tray_command_path || pc.tray_command_path || "")}"></label>
      </div>
      <label>PDF 下载/扫描目录<textarea id="localDownloadDirs" rows="3">${h(localDownloadDirs)}</textarea></label>
      <div class="toolbar inlineToolbar">
        <button id="restartAdminBtn" class="primary" ${local.process_running ? "" : "disabled"}>重启后台服务</button>
        <span id="adminRuntimeStatus" class="label">${local.process_running ? "命令将由系统托盘中的 SciToday_admin 执行" : "请先启动 SciToday_admin.exe"}</span>
      </div>
      <div class="label">Host、Port、数据目录和下载目录保存后，需要重启后台才会完全生效。</div>
    </div>
    <div class="panel">
      <h2>PC / Quick Tunnel</h2>
      <div class="grid">
        ${copyField("当前服务器 URL", currentUrl, "尚未生成服务器 URL")}
        ${copyField("服务器 Token", authToken, "未启用 Token")}
        ${copyField("Quick Tunnel URL（备用）", quickTunnel.url || "", "Quick Tunnel 未连接")}
        <label>数据目录<input readonly value="${h(pc.data_dir || "")}"></label>
        <label>配置文件<input readonly value="${h(pc.config_path || "")}"></label>
        <label>Inbox<input readonly value="${h(pc.inbox_dir || "")}"></label>
        <label>PDF 上传目录<input readonly value="${h(pc.uploaded_pdf_dir || "")}"></label>
        <label>状态文件<input readonly value="${h(pc.quick_tunnel_state_path || "")}"></label>
      </div>
      <label>PDF 扫描目录<textarea readonly rows="3">${h((pc.download_dirs || []).join("\n"))}</textarea></label>
      <pre>${h(JSON.stringify(quickTunnel, null, 2))}</pre>
    </div>
    <button id="saveSettingsBtn" class="primary">保存设置</button>`;
  const enabled = String((cfg.schedule || {}).enabled ?? true);
  document.getElementById("scheduleEnabled").value = enabled;
}

function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${units[i]}`;
}

function formatUptime(seconds) {
  const s = Number(seconds || 0);
  const d = Math.floor(s / 86400);
  const hrs = Math.floor((s % 86400) / 3600);
  const min = Math.floor((s % 3600) / 60);
  if (d) return `${d}天 ${hrs}小时`;
  if (hrs) return `${hrs}小时 ${min}分`;
  return `${min}分`;
}

function renderMetrics(m) {
  const storage = m.storage || {};
  const coord = m.coordinator || {};
  const storageCard = card("数据规模", `
    <div class="label">数据根目录</div>
    <div class="metricRow"><span>控制库</span><b>${formatBytes(storage.control_db_bytes)}</b></div>
    <div class="metricRow"><span>共享内容库</span><b>${formatBytes(storage.shared_content_db_bytes)}</b></div>
    <div class="metricRow"><span>租户数据</span><b>${formatBytes(storage.tenants_bytes)}</b></div>
    <div class="metricRow"><span>数据根合计</span><b>${formatBytes(storage.data_root_bytes)}</b></div>
    <div class="metricRow"><span>租户数量</span><b>${h(m.tenant_count ?? "-")}</b></div>
    ${coord.max_workers !== undefined ? `<div class="metricRow"><span>任务并发/队列</span><b>${h(coord.max_workers)} / ${h(coord.max_pending)}</b></div>` : ""}`);

  if (!m.available) {
    return `<div class="grid">
      ${card("后端性能", `<div class="stat warn">不可用</div><div class="label">${h(m.message || "未安装 psutil")}</div>`)}
      ${storageCard}
    </div>`;
  }
  const sys = m.system || {};
  const proc = m.process || {};
  const disk = m.disk || {};
  const handles = proc.num_handles !== undefined ? proc.num_handles
    : (proc.num_fds !== undefined ? proc.num_fds : "-");
  return `<div class="grid">
    ${card("进程内存 (RSS)", `<div class="stat">${formatBytes(proc.memory_rss_bytes)}</div><div class="label">虚拟内存 ${formatBytes(proc.memory_vms_bytes)} · PID ${h(proc.pid ?? "-")}</div>`)}
    ${card("系统内存", `<div class="stat ${sys.memory_percent > 90 ? "bad" : sys.memory_percent > 75 ? "warn" : "ok"}">${h(Math.round(sys.memory_percent || 0))}%</div><div class="label">${formatBytes(sys.memory_used_bytes)} / ${formatBytes(sys.memory_total_bytes)}</div>`)}
    ${card("CPU", `<div class="stat">${h((proc.cpu_percent ?? 0).toFixed ? proc.cpu_percent.toFixed(1) : proc.cpu_percent)}%</div><div class="label">系统 ${h(sys.cpu_percent ?? 0)}% · ${h(sys.cpu_count || "-")} 核</div>`)}
    ${card("运行时长", `<div class="stat">${formatUptime(proc.uptime_seconds)}</div><div class="label">启动于 ${h(proc.started_at || "-")}</div>`)}
    ${card("线程 / 句柄", `<div class="stat">${h(proc.num_threads ?? "-")}</div><div class="label">句柄/FD ${h(handles)}</div>`)}
    ${card("数据盘", `<div class="stat ${disk.percent > 90 ? "bad" : disk.percent > 75 ? "warn" : "ok"}">${h(Math.round(disk.percent || 0))}%</div><div class="label">${formatBytes(disk.used_bytes)} / ${formatBytes(disk.total_bytes)}</div>`)}
    ${storageCard}
  </div>`;
}

async function renderMonitor() {
  const [o, feeds, events, metrics] = await Promise.all([
    api("/api/admin/overview"),
    api("/api/admin/feed-health"),
    api("/api/admin/events?limit=100"),
    api("/api/admin/metrics").catch(() => ({available: false, message: "无法获取性能指标"})),
  ]);
  const tunnelUrl = o.tunnel.current_url || ((o.tunnel.quick || {}).url) || o.tunnel.configured_url || "";
  const pathRows = Object.entries(o.paths || {}).map(([k, v]) =>
    `<tr><td>${h(k)}</td><td>${h(v.path)}</td><td class="${v.exists ? "ok" : "warn"}">${v.exists ? "存在" : "不存在"}</td><td class="${v.writable ? "ok" : "bad"}">${v.writable ? "可写" : "不可写"}</td></tr>`
  ).join("");
  document.getElementById("monitor").innerHTML = `
    ${collapsibleCard("monitor-performance", "后端性能", renderMetrics(metrics), {open: true, meta: metrics.available ? "psutil" : "不可用"})}
    <div class="grid">
      ${collapsibleCard(
        "monitor-app-tunnel",
        "App -> Tunnel",
        `<div class="${o.app.online ? "ok" : "warn"}">${o.app.online ? "App 最近在线" : "App 心跳过期或不存在"}</div><pre>${h(JSON.stringify(o.app.payload || {}, null, 2))}</pre>`,
        {meta: o.app.online ? "在线" : "离线"}
      )}
      ${collapsibleCard(
        "monitor-tunnel-flask",
        "Tunnel -> Flask",
        `<div class="${o.tunnel.process_running ? "ok" : "warn"}">${o.tunnel.mode === "named" ? "Named Tunnel" : "Quick Tunnel"}: ${o.tunnel.process_running ? "运行" : "未连接"}</div><div>${h(tunnelUrl || "Tunnel URL 未生成")}</div>${o.tunnel.cloudflared_process_present && o.tunnel.mode !== "named" ? `<div class="label">检测到系统 cloudflared 进程/服务，可能是旧 MSI/Service。</div>` : ""}<pre>${h(JSON.stringify(o.tunnel.quick || {}, null, 2))}</pre>`,
        {meta: o.tunnel.process_running ? "已连接" : "未连接"}
      )}
      ${collapsibleCard(
        "monitor-flask-db",
        "Flask -> DB",
        `<div class="${o.server.listening ? "ok" : "bad"}">${h(o.server.local_url)} ${o.server.listening ? "可访问" : "不可访问"}</div>`,
        {meta: o.server.listening ? "正常" : "异常"}
      )}
    </div>
    ${collapsibleCard(
      "monitor-paths",
      "路径检查",
      `<div class="tableWrap"><table><thead><tr><th>项目</th><th>路径</th><th>存在</th><th>可写</th></tr></thead><tbody>${pathRows}</tbody></table></div>`,
      {meta: `${Object.keys(o.paths || {}).length} 项`}
    )}
    ${collapsibleCard(
      "monitor-feed-health",
      "RSS 源健康",
      `<div class="tableWrap"><table><thead><tr><th>源</th><th>状态</th><th>HTTP</th><th>最近成功</th><th>下次抓取 / 封禁至</th><th>错误</th><th>操作</th></tr></thead><tbody>${feeds.map(f => `<tr>
        <td>${h(f.title)}<div class="label">${h(f.host || "")}</div></td>
        <td class="${f.status === "ok" ? "ok" : f.status === "blocked" ? "warn" : "bad"}">${h(f.status)}${f.consecutive_failures ? ` (${h(f.consecutive_failures)})` : ""}</td>
        <td>${h(f.http_status || "-")}</td>
        <td>${h(f.last_ok || "-")}</td>
        <td>${h(f.blocked_until || f.next_fetch || "-")}</td>
        <td>${h(f.error_category || "")}<div class="label">${h(f.error || "")}</div></td>
        <td><button class="rssProbe" data-url="${h(f.url)}" ${f.probe_allowed === false ? "disabled" : ""}>单次探测</button></td>
      </tr>`).join("")}</tbody></table></div>`,
      {meta: `${feeds.length} 个源`}
    )}
    ${collapsibleCard("monitor-events", "事件", renderEvents(events), {meta: `${events.length} 条`})}
  `;
  document.querySelectorAll(".rssProbe").forEach(button => {
    button.addEventListener("click", async () => {
      const url = button.dataset.url;
      if (!confirm(`将对该源发送一次请求并绕过当前抓取冷却；同域名一小时内只能执行一次。\\n\\n${url}`)) return;
      button.disabled = true;
      try {
        const result = await api("/api/admin/rss-probe", {
          method: "POST",
          body: JSON.stringify({url, override_cooldown: true}),
        });
        alert(result.ok
          ? `探测成功：HTTP ${result.upstream_status || "-"}，新增 ${result.new_items || 0} 篇`
          : `探测完成：${result.category || result.error || "未知错误"}，HTTP ${result.upstream_status || "-"}`);
        await renderMonitor();
      } catch (error) {
        alert(`探测失败：${error.message}`);
        button.disabled = false;
      }
    });
  });
}

async function renderLogs() {
  const logs = await api("/api/logs?lines=400");
  document.getElementById("logs").innerHTML = collapsibleCard(
    "backend-logs",
    "后端日志",
    `<pre>${h((logs || []).join("\n"))}</pre>`,
    {meta: `${(logs || []).length} 行`}
  );
}

async function refresh() {
  try {
    if (state.view === "dashboard") await renderDashboard();
    if (state.view === "messages") await renderDigests("messages", "rss");
    if (state.view === "reading") await renderDigests("reading", "pdf");
    if (state.view === "feeds") await renderFeeds();
    if (state.view === "settings") await renderSettings();
    if (state.view === "monitor") await renderMonitor();
    if (state.view === "logs") await renderLogs();
  } catch (e) {
    if (e.message !== "unauthorized") {
      console.error(e);
      // 渲染失败时，在当前视图里显式提示，避免页面完全空白无从排查。
      const container = document.getElementById(state.view);
      if (container && !container.innerHTML.trim()) {
        container.innerHTML = `<div class="panel"><div class="label bad">页面加载失败：${h(e.message)}</div></div>`;
      }
    }
  }
}

async function openDigest(filename, title) {
  state.currentDigest = filename;
  document.getElementById("detailTitle").textContent = title || filename;
  document.getElementById("digestFrame").src = withToken(`/inbox/${encodeURIComponent(filename)}`);
  document.getElementById("chatOutput").textContent = "";
  document.getElementById("detailDialog").showModal();
}

async function refreshTunnelUrl() {
  const button = document.getElementById("refreshTunnelBtn");
  const status = document.getElementById("tunnelRefreshStatus");
  if (button) button.disabled = true;
  if (status) status.textContent = "正在请求刷新...";
  const before = await api("/api/admin/overview");
  const previousUrl = ((before.tunnel || {}).current_url) || "";
  await api("/api/admin/tunnel/refresh", {method: "POST", body: "{}"});

  for (let i = 0; i < 40; i += 1) {
    await sleep(1500);
    const overview = await api("/api/admin/overview");
    const tunnel = overview.tunnel || {};
    const url = tunnel.current_url || ((tunnel.quick || {}).url) || tunnel.configured_url || "";
    const tunnelStatus = (tunnel.quick || {}).status || "";
    if (status) status.textContent = tunnelStatus || "刷新中...";
    if (url && (url !== previousUrl || tunnelStatus === "connected")) {
      await renderDashboard();
      return;
    }
  }
  if (status) status.textContent = "仍在等待 Quick Tunnel 生成 URL";
  if (button) button.disabled = false;
}

async function restartAdminBackend() {
  if (!window.confirm("确定重启 SciToday_admin 后台服务吗？正在运行的任务可能被中断。")) return;
  const button = document.getElementById("restartAdminBtn");
  const status = document.getElementById("adminRuntimeStatus");
  if (button) button.disabled = true;
  if (status) status.textContent = "重启命令已发送，等待服务恢复...";
  await api("/api/admin/runtime/restart_backend", {method: "POST", body: "{}"});
  let sawOffline = false;
  for (let i = 0; i < 40; i += 1) {
    await sleep(500);
    try {
      const response = await fetch("/healthz", {cache: "no-store"});
      if (!response.ok) {
        sawOffline = true;
        continue;
      }
      if (sawOffline || i >= 5) {
        if (status) status.textContent = "后台服务已恢复";
        await renderSettings();
        return;
      }
    } catch (_) {
      sawOffline = true;
    }
  }
  if (status) status.textContent = "尚未确认服务恢复，请检查系统托盘图标";
  if (button) button.disabled = false;
}

document.addEventListener("click", async e => {
  const nav = e.target.closest(".nav");
  if (nav) setView(nav.dataset.view);
  if (e.target.id === "refreshTunnelBtn") await refreshTunnelUrl();
  if (e.target.id === "restartAdminBtn") await restartAdminBackend();
  if (e.target.id === "saveAppConnectionBtn") {
    const serverUrl = document.getElementById("appServerUrl").value.trim();
    await api("/api/admin/settings", {
      method: "POST",
      body: JSON.stringify({pc: {cloudflare_tunnel_url: serverUrl}}),
    });
    await renderDashboard();
  }
  if (e.target.id === "saveTokenBtn") {
    state.token = document.getElementById("tokenInput").value.trim();
    localStorage.setItem("rssaiToken", state.token);
    await api("/api/admin/session", {method: "POST", body: JSON.stringify({token: state.token})});
    document.getElementById("loginPanel").classList.add("hidden");
    await loadPrincipal();
    refresh();
  }
  if (e.target.dataset.open) openDigest(e.target.dataset.open, e.target.dataset.title);
  if (e.target.dataset.action === "runRss") await api("/api/run/rss", {method: "POST", body: "{}"}).then(refresh);
  if (e.target.dataset.action === "runPdf") await api("/api/run/pdf", {method: "POST", body: "{}"}).then(refresh);
  if (e.target.dataset.action === "discovery") await api("/api/admin/run/rss-discovery", {method: "POST", body: "{}"}).then(refresh);
  if (e.target.dataset.action === "publish") await api("/api/admin/run/rss-publish", {method: "POST", body: "{}"}).then(refresh);
  if (e.target.dataset.action === "cleanupRss") await cleanupSource("rss");
  if (e.target.dataset.action === "cleanupPdf") await cleanupSource("pdf");
  if (e.target.id === "closeDetailBtn") document.getElementById("detailDialog").close();
  if (e.target.id === "closeTokenBtn") document.getElementById("tokenDialog").close();
  if (e.target.id === "addTenantBtn") {
    const name = document.getElementById("newTenantName").value.trim();
    if (!name) { alert("请输入租户显示名称"); return; }
    const scopes = selectedTenantScopes();
    if (!scopes.length) { alert("请至少选择一个 scope"); return; }
    try {
      const r = await api("/api/admin/tenants", {method: "POST", body: JSON.stringify({display_name: name, scopes})});
      showTokenDialog(`租户 ${r.tenant.display_name} 的 Token`, r.token, r.token_meta);
      await renderSettings();
    } catch (err) {
      alert(`创建失败：${err.message}`);
    }
  }
  if (e.target.dataset.deleteTenant) {
    const id = e.target.dataset.deleteTenant;
    const name = e.target.dataset.tenantName || id;
    if (!window.confirm(`确定删除租户「${name}」吗？\n\n将立即吊销其全部 Token 并停止其调度，数据目录会保留，可稍后彻底删除或人工恢复。`)) return;
    try {
      await api(`/api/admin/tenants/${encodeURIComponent(id)}/delete`, {method: "POST", body: "{}"});
      await renderSettings();
    } catch (err) {
      alert(`删除失败：${err.message}`);
    }
  }
  if (e.target.dataset.purgeTenant) {
    const id = e.target.dataset.purgeTenant;
    const name = e.target.dataset.tenantName || id;
    if (!window.confirm(`确定彻底删除租户「${name}」吗？\n\n此操作不可逆：会先把该租户数据备份成 zip 到 control/backups，然后永久删除其数据目录和记录。`)) return;
    try {
      const r = await api(`/api/admin/tenants/${encodeURIComponent(id)}/purge`, {method: "POST", body: "{}"});
      alert(`已彻底删除，备份文件：\n${r.backup_path}`);
      await renderSettings();
    } catch (err) {
      alert(`彻底删除失败：${err.message}`);
    }
  }
  if (e.target.dataset.addToken) {
    const id = e.target.dataset.addToken;
    const name = e.target.dataset.tenantName || id;
    const scopes = selectedTenantScopes();
    if (!scopes.length) { alert("请在下方“添加租户”处勾选要授予的 scope，再新增 Token"); return; }
    try {
      const r = await api(`/api/admin/tenants/${encodeURIComponent(id)}/tokens`, {method: "POST", body: JSON.stringify({scopes})});
      showTokenDialog(`租户 ${name} 的新 Token`, r.token, r.token_meta);
      await renderSettings();
    } catch (err) {
      alert(`新增 Token 失败：${err.message}`);
    }
  }
  if (e.target.id === "chatSendBtn") {
    const text = document.getElementById("chatInput").value.trim();
    if (!text || !state.currentDigest) return;
    const out = document.getElementById("chatOutput");
    out.textContent = "思考中...";
    const r = await api("/api/chat", {method: "POST", body: JSON.stringify({filename: state.currentDigest, message: text, history: []})});
    out.textContent = r.reply || r.error || "";
  }
  if (e.target.id === "addFeedBtn") {
    await api("/api/feeds", {method: "POST", body: JSON.stringify({title: document.getElementById("feedTitle").value, url: document.getElementById("feedUrl").value})});
    renderFeeds();
  }
  if (e.target.id === "importOpmlBtn") {
    const input = document.getElementById("opmlImportFile");
    const file = input && input.files ? input.files[0] : null;
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    await api("/api/feeds/import", {method: "POST", body: form});
    renderFeeds();
  }
  if (e.target.dataset.updateFeed) {
    const index = e.target.dataset.feedIndex;
    await api("/api/feeds", {
      method: "PATCH",
      body: JSON.stringify({
        old_url: decodeURIComponent(e.target.dataset.updateFeed),
        title: document.getElementById(`feedTitleEdit${index}`).value,
        url: document.getElementById(`feedUrlEdit${index}`).value,
      }),
    });
    renderFeeds();
  }
  if (e.target.dataset.deleteFeed) {
    await api(`/api/feeds/${e.target.dataset.deleteFeed}`, {method: "DELETE"});
    renderFeeds();
  }
  if (e.target.id === "resetRssTimeBtn") {
    const days = Number(document.getElementById("rssLookbackDays").value || 7);
    await api("/api/admin/rss-reset-time", {
      method: "POST",
      body: JSON.stringify({lookback_days: days}),
    });
    await renderSettings();
  }
  if (e.target.id === "saveSettingsBtn") {
    const preferenceWeights = {
      pdf_matched: Number(document.getElementById("pdfMatchedWeight").value),
      interested: Number(document.getElementById("interestedWeight").value),
      is_read: Number(document.getElementById("readWeight").value),
      disliked: Number(document.getElementById("dislikedWeight").value),
    };
    const positiveWeights = [
      preferenceWeights.pdf_matched,
      preferenceWeights.interested,
      preferenceWeights.is_read,
    ];
    if (positiveWeights.some(value => !Number.isFinite(value) || value < 0 || value > 100)
      || !Number.isFinite(preferenceWeights.disliked)
      || preferenceWeights.disliked < -100
      || preferenceWeights.disliked > 0) {
      alert("正向权重必须为 0 到 100，不喜欢权重必须为 -100 到 0");
      return;
    }
    if (!(preferenceWeights.pdf_matched >= preferenceWeights.interested
      && preferenceWeights.interested >= preferenceWeights.is_read
      && preferenceWeights.is_read >= 0
      && preferenceWeights.disliked <= 0)) {
      alert("权重必须满足：PDF匹配 ≥ 感兴趣 ≥ 已读 ≥ 0 ≥ 不喜欢");
      return;
    }
    const body = {
      schedule: {
        rss_discovery_interval_minutes: Number(document.getElementById("rssDiscoveryInterval").value || 60),
        rss_interval_minutes: Number(document.getElementById("rssInterval").value || 30),
        pdf_interval_minutes: Number(document.getElementById("pdfInterval").value || 5),
        enabled: document.getElementById("scheduleEnabled").value === "true",
      },
      rss: {
        opml_path: document.getElementById("rssOpmlPath").value,
        per_feed_limit: Number(document.getElementById("perFeedLimit").value || 3),
        max_push_items: Number(document.getElementById("maxPushItems").value || 20),
        lookback_days: Number(document.getElementById("rssLookbackDays").value || 7),
        preference_weights: preferenceWeights,
        ...readRssFetchSettings(),
      },
      ai: {
        api_key: document.getElementById("aiKey").value,
        base_url: document.getElementById("aiBaseUrl").value,
        model: document.getElementById("aiModel").value,
        system_prompt: document.getElementById("systemPrompt").value,
        rss_prompt: document.getElementById("rssPrompt").value,
        pdf_prompt: document.getElementById("pdfPrompt").value,
      },
      server: {
        host: document.getElementById("serverHost").value,
        port: Number(document.getElementById("serverPort").value || 5000),
        auth_token: document.getElementById("serverAuthToken").value,
      },
      pc: {
        cloudflare_tunnel_url: document.getElementById("serverPublicUrl").value,
      },
    };
    await api("/api/admin/settings", {method: "POST", body: JSON.stringify(body)});
    await api("/api/admin/local-settings", {method: "POST", body: JSON.stringify({
      local: {
        startup_enabled: document.getElementById("startupEnabled").checked,
        host: document.getElementById("serverHost").value,
        port: Number(document.getElementById("serverPort").value || 5200),
        data_dir: document.getElementById("localDataDir").value,
        server_data_dir: document.getElementById("serverDataDir").value,
        download_dirs: splitDirs(document.getElementById("localDownloadDirs").value),
        tunnel_mode: "Quick",
        tunnel_enabled: document.getElementById("tunnelEnabled").checked,
        tunnel_url: document.getElementById("serverPublicUrl").value,
      }
    })});
    refresh();
  }
  if (e.target.dataset.copy !== undefined) {
    const value = e.target.dataset.copy || "";
    if (!value) return;
    await navigator.clipboard.writeText(value);
    const old = e.target.textContent;
    e.target.textContent = "已复制";
    setTimeout(() => { e.target.textContent = old; }, 1000);
  }
});

document.addEventListener("toggle", e => {
  const detail = e.target.closest && e.target.closest("details[data-collapse-key]");
  if (!detail || detail !== e.target) return;
  sessionStorage.setItem(`collapse:${detail.dataset.collapseKey}`, detail.open ? "open" : "closed");
}, true);

setInterval(() => {
  if (["dashboard", "messages", "reading", "monitor"].includes(state.view)) refresh();
}, 15000);

async function bootstrap() {
  await loadPrincipal();
  const initialView = initialParams.get("view");
  if (initialView && titles[initialView]) setView(initialView);
  else refresh();
}
bootstrap();
