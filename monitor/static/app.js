"use strict";

const state = {
  data: null,
  view: "prs",
  prFilter: "all",
  outboxFilter: "attention",
  query: "",
  retryId: null,
  toastTimer: null,
};

const elements = {
  serviceState: document.querySelector("#serviceState"),
  serviceStateText: document.querySelector("#serviceStateText"),
  scanButton: document.querySelector("#scanButton"),
  scanMeta: document.querySelector("#scanMeta"),
  metricOpen: document.querySelector("#metricOpen"),
  metricFailing: document.querySelector("#metricFailing"),
  metricPending: document.querySelector("#metricPending"),
  metricAttention: document.querySelector("#metricAttention"),
  notice: document.querySelector("#notice"),
  noticeTitle: document.querySelector("#noticeTitle"),
  noticeBody: document.querySelector("#noticeBody"),
  loadingState: document.querySelector("#loadingState"),
  prView: document.querySelector("#prView"),
  outboxView: document.querySelector("#outboxView"),
  prRows: document.querySelector("#prRows"),
  outboxRows: document.querySelector("#outboxRows"),
  prTabCount: document.querySelector("#prTabCount"),
  outboxTabCount: document.querySelector("#outboxTabCount"),
  searchInput: document.querySelector("#searchInput"),
  prFilters: document.querySelector("#prFilters"),
  outboxFilters: document.querySelector("#outboxFilters"),
  prDialog: document.querySelector("#prDialog"),
  prDialogTitle: document.querySelector("#prDialogTitle"),
  prDialogBody: document.querySelector("#prDialogBody"),
  prDialogLink: document.querySelector("#prDialogLink"),
  retryDialog: document.querySelector("#retryDialog"),
  retryDialogText: document.querySelector("#retryDialogText"),
  confirmRetryButton: document.querySelector("#confirmRetryButton"),
  toast: document.querySelector("#toast"),
};

const statusNames = {
  pending: "待发送",
  sent: "已发送",
  failed: "发送失败",
  dead: "重试耗尽",
  uncertain: "结果不确定",
  unmapped: "未映射",
};

document.querySelectorAll("[data-view]").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});

elements.prFilters.addEventListener("click", (event) => setFilter(event, "prFilter"));
elements.outboxFilters.addEventListener("click", (event) => setFilter(event, "outboxFilter"));
elements.searchInput.addEventListener("input", (event) => {
  state.query = event.target.value.trim().toLowerCase();
  renderCurrentView();
});
elements.scanButton.addEventListener("click", requestScan);
elements.confirmRetryButton.addEventListener("click", confirmRetry);

document.addEventListener("click", (event) => {
  const closeButton = event.target.closest("[data-close-dialog]");
  if (closeButton) document.querySelector(`#${closeButton.dataset.closeDialog}`).close();

  const detailButton = event.target.closest("[data-pr-detail]");
  if (detailButton) openPrDialog(Number(detailButton.dataset.prDetail));

  const retryButton = event.target.closest("[data-retry]");
  if (retryButton) openRetryDialog(Number(retryButton.dataset.retry));
});

[elements.prDialog, elements.retryDialog].forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

loadDashboard();
window.setInterval(() => loadDashboard(true), 5000);

async function loadDashboard(silent = false) {
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    state.data = await response.json();
    render();
  } catch (error) {
    if (!silent || !state.data) {
      elements.loadingState.hidden = false;
      elements.loadingState.innerHTML = emptyMarkup("alert", "无法读取监控状态", String(error));
    }
    setServiceState("danger", "仪表盘断开");
  }
}

function render() {
  const data = state.data;
  elements.loadingState.hidden = true;
  elements.metricOpen.textContent = formatNumber(data.metrics.open_prs);
  elements.metricFailing.textContent = formatNumber(data.metrics.failing_prs);
  elements.metricPending.textContent = formatNumber(data.metrics.pending_delivery);
  elements.metricAttention.textContent = formatNumber(data.metrics.delivery_attention);
  elements.prTabCount.textContent = formatNumber(data.pull_requests.length);
  elements.outboxTabCount.textContent = formatNumber(data.metrics.delivery_attention);
  renderHealth(data);
  renderCurrentView();
}

function renderHealth(data) {
  const scan = data.scan;
  const runtime = data.runtime || {};
  elements.scanButton.classList.toggle("scanning", Boolean(runtime.scanning));
  elements.scanButton.disabled = Boolean(runtime.scanning || runtime.scan_requested);
  elements.scanButton.querySelector("span").textContent = runtime.scanning
    ? "扫描中"
    : runtime.scan_requested
      ? "已安排"
      : "立即扫描";

  if (runtime.scanning) {
    setServiceState("scanning", "正在扫描");
  } else if (!scan) {
    setServiceState("idle", "等待首次扫描");
  } else if (scan.errors.length) {
    setServiceState("danger", "扫描有错误");
  } else if (isStale(scan.completed_at)) {
    setServiceState("warning", "监控已停滞");
  } else {
    setServiceState("healthy", "运行正常");
  }

  elements.scanMeta.textContent = scan
    ? `最近扫描 ${relativeTime(scan.completed_at)} · ${Number(scan.duration_seconds).toFixed(1)} 秒 · ${scan.scanned_prs}/${scan.open_prs} 个 PR`
    : "尚未完成首次扫描";

  const notice = healthNotice(data);
  elements.notice.hidden = !notice;
  if (notice) {
    elements.notice.dataset.tone = notice.tone;
    elements.noticeTitle.textContent = notice.title;
    elements.noticeBody.textContent = notice.body;
  }
}

function healthNotice(data) {
  const scan = data.scan;
  if (scan && scan.errors.length) {
    return {
      tone: "danger",
      title: `最近扫描有 ${scan.errors.length} 个错误`,
      body: scan.errors[0],
    };
  }
  if (scan && isStale(scan.completed_at)) {
    return { tone: "warning", title: "监控超过 7 分钟没有完成扫描", body: "请检查进程、Gitea 网络和凭据状态。" };
  }
  if (data.metrics.delivery_attention) {
    return { tone: "warning", title: `${data.metrics.delivery_attention} 条消息需要处理`, body: "打开“消息发送”查看失败、超时或未映射记录。" };
  }
  return null;
}

function setView(view) {
  if (state.view === view) return;
  state.view = view;
  document.querySelectorAll("[data-view]").forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  elements.prFilters.hidden = view !== "prs";
  elements.outboxFilters.hidden = view !== "outbox";
  elements.searchInput.placeholder = view === "prs" ? "搜索 PR、标题或提交人" : "搜索 PR、收件人或消息";
  renderCurrentView();
}

function setFilter(event, key) {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  state[key] = button.dataset.filter;
  button.parentElement.querySelectorAll("[data-filter]").forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  });
  renderCurrentView();
}

function renderCurrentView() {
  if (!state.data) return;
  const showPrs = state.view === "prs";
  elements.prView.hidden = !showPrs;
  elements.outboxView.hidden = showPrs;
  if (showPrs) renderPullRequests();
  else renderOutbox();
}

function renderPullRequests() {
  let items = state.data.pull_requests;
  if (state.prFilter === "failing") items = items.filter((item) => item.failures.length);
  if (state.prFilter === "clear") items = items.filter((item) => !item.failures.length);
  if (state.query) {
    items = items.filter((item) => [item.number, item.title, item.author, item.head_sha].join(" ").toLowerCase().includes(state.query));
  }
  if (!items.length) {
    elements.prRows.innerHTML = emptyMarkup("inbox", "没有匹配的 PR", "调整搜索或筛选条件");
    return;
  }
  elements.prRows.innerHTML = items.map((item) => {
    const failure = item.failures[0];
    const status = failure
      ? `<div class="status-stack"><span class="status-mark danger"><svg class="icon" aria-hidden="true"><use href="#icon-alert"></use></svg></span><button class="row-detail-button status-copy" type="button" data-pr-detail="${item.number}"><strong>${escapeHtml(failure.context)}${item.failures.length > 1 ? ` +${item.failures.length - 1}` : ""}</strong><span>${escapeHtml(oneLine(failure.summary))}</span></button></div>`
      : `<div class="status-stack"><span class="status-mark success"><svg class="icon" aria-hidden="true"><use href="#icon-check"></use></svg></span><div class="status-copy"><strong>未发现关键失败</strong><span>当前 head 最新结果</span></div></div>`;
    return `<article class="data-row pr-grid ${failure ? "has-failure" : ""}">
      <div class="cell primary-cell"><a class="pr-link" href="${escapeAttribute(safeUrl(item.url))}" target="_blank" rel="noreferrer"><span>#${item.number} · ${escapeHtml(item.title || "无标题")}</span><svg class="icon" aria-hidden="true"><use href="#icon-external"></use></svg></a><div class="subline mono">${escapeHtml(item.head_sha.slice(0, 12))}</div></div>
      <div class="cell"><span class="cell-label">提交人</span><strong>${escapeHtml(item.author)}</strong></div>
      <div class="cell"><span class="cell-label">当前命令</span><span class="mono">${escapeHtml(item.latest_ci_command || "--")}</span><div class="subline">${item.latest_ci_command_at ? relativeTime(item.latest_ci_command_at) : "暂无命令"}</div></div>
      <div class="cell">${status}</div>
      <div class="cell time-cell"><span class="cell-label">最近扫描</span>${relativeTime(item.scanned_at)}</div>
    </article>`;
  }).join("");
}

function renderOutbox() {
  const attention = new Set(["failed", "dead", "uncertain", "unmapped"]);
  const pending = new Set(["pending", "failed", "dead", "uncertain", "unmapped"]);
  let items = state.data.outbox;
  if (state.outboxFilter === "attention") items = items.filter((item) => attention.has(item.status));
  if (state.outboxFilter === "pending") items = items.filter((item) => pending.has(item.status));
  if (state.query) {
    items = items.filter((item) => [item.pr_number, item.author, item.receiver, item.message].join(" ").toLowerCase().includes(state.query));
  }
  if (!items.length) {
    const title = state.outboxFilter === "attention" ? "没有需要处理的消息" : "没有匹配的发送记录";
    elements.outboxRows.innerHTML = emptyMarkup("inbox", title, "发送状态会在这里持续更新");
    return;
  }
  elements.outboxRows.innerHTML = items.map((item) => {
    const retryable = ["failed", "dead", "uncertain", "unmapped"].includes(item.status);
    return `<article class="data-row outbox-grid">
      <div class="cell primary-cell"><strong>#${item.id} · PR #${item.pr_number}</strong><div class="subline" title="${escapeAttribute(item.message)}">${escapeHtml(oneLine(item.message))}</div></div>
      <div class="cell"><span class="cell-label">收件人</span><strong>${escapeHtml(item.receiver || item.author || "--")}</strong></div>
      <div class="cell"><span class="status-badge ${escapeAttribute(item.status)}">${escapeHtml(statusNames[item.status] || item.status)}</span>${item.last_error ? `<div class="subline" title="${escapeAttribute(item.last_error)}">${escapeHtml(oneLine(item.last_error))}</div>` : ""}</div>
      <div class="cell mono"><span class="cell-label">尝试</span>${item.attempts}</div>
      <div class="cell time-cell"><span class="cell-label">更新时间</span>${relativeTime(item.updated_at)}</div>
      <div class="cell row-action">${retryable ? `<button class="icon-button" type="button" data-retry="${item.id}" aria-label="重试消息 #${item.id}" title="重新加入发送队列"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg></button>` : ""}</div>
    </article>`;
  }).join("");
}

function openPrDialog(number) {
  const item = state.data.pull_requests.find((pr) => pr.number === number);
  if (!item) return;
  elements.prDialogTitle.textContent = `#${item.number} · ${item.title || "无标题"}`;
  elements.prDialogLink.href = safeUrl(item.url);
  const failures = item.failures.length
    ? item.failures.map((failure) => `<section class="failure-detail"><strong>${escapeHtml(failure.context)}</strong><p>${escapeHtml(failure.summary)}</p><div class="subline">${formatDateTime(failure.updated_at)}</div></section>`).join("")
    : `<div class="empty-state"><svg class="icon" aria-hidden="true"><use href="#icon-check"></use></svg><strong>未发现关键失败</strong></div>`;
  elements.prDialogBody.innerHTML = `<div class="dialog-meta"><span>提交人 ${escapeHtml(item.author)}</span><span class="mono">Head ${escapeHtml(item.head_sha.slice(0, 12))}</span><span>${escapeHtml(item.latest_ci_command || "暂无 CI 命令")}</span></div>${failures}`;
  elements.prDialog.showModal();
}

function openRetryDialog(id) {
  const item = state.data.outbox.find((record) => record.id === id);
  if (!item) return;
  state.retryId = id;
  elements.retryDialogText.textContent = item.status === "uncertain"
    ? `消息 #${id} 可能已经送达。再次发送可能让提交人收到重复消息。`
    : `消息 #${id} 将清空失败计数，并在下一轮重新发送。`;
  elements.retryDialog.showModal();
}

async function confirmRetry() {
  if (!state.retryId) return;
  elements.confirmRetryButton.disabled = true;
  try {
    const response = await fetch(`/api/outbox/${state.retryId}/retry`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Monitor-Action": "1" },
      body: JSON.stringify({ confirm: true }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    elements.retryDialog.close();
    showToast("消息已重新加入发送队列");
    await loadDashboard(true);
  } catch (error) {
    showToast(`重试失败：${error}`, true);
  } finally {
    elements.confirmRetryButton.disabled = false;
    state.retryId = null;
  }
}

async function requestScan() {
  elements.scanButton.disabled = true;
  try {
    const response = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Monitor-Action": "1" },
      body: "{}",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    showToast(payload.accepted ? "已安排立即扫描" : "扫描已在等待队列中");
    await loadDashboard(true);
  } catch (error) {
    elements.scanButton.disabled = false;
    showToast(`无法安排扫描：${error}`, true);
  }
}

function setServiceState(tone, label) {
  elements.serviceState.dataset.tone = tone;
  elements.serviceStateText.textContent = label;
}

function showToast(message, isError = false) {
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  elements.toast.classList.add("visible");
  state.toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 2600);
}

function emptyMarkup(icon, title, body) {
  return `<div class="empty-state"><svg class="icon" aria-hidden="true"><use href="#icon-${icon}"></use></svg><strong>${escapeHtml(title)}</strong><span>${escapeHtml(body)}</span></div>`;
}

function oneLine(value) {
  const normalized = String(value || "").replace(/\s+/g, " ").trim();
  return normalized.length > 180 ? `${normalized.slice(0, 179).trim()}…` : normalized;
}

function safeUrl(value) {
  try {
    const parsed = new URL(String(value), window.location.origin);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "#";
  } catch (_) {
    return "#";
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
}

function escapeAttribute(value) { return escapeHtml(value); }
function formatNumber(value) { return new Intl.NumberFormat("zh-CN").format(Number(value || 0)); }

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "--";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
}

function relativeTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "--";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });
  if (Math.abs(seconds) < 60) return formatter.format(seconds, "second");
  const minutes = Math.round(seconds / 60);
  if (Math.abs(minutes) < 60) return formatter.format(minutes, "minute");
  const hours = Math.round(minutes / 60);
  if (Math.abs(hours) < 24) return formatter.format(hours, "hour");
  return formatDateTime(value);
}

function isStale(value) {
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) && Date.now() - timestamp > 7 * 60 * 1000;
}
