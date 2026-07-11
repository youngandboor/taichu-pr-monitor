"use strict";

const state = {
  data: null,
  view: "prs",
  prFilter: "all",
  outboxFilter: "all",
  query: "",
  retryId: null,
  detailId: null,
  action: null,
  toastTimer: null,
};

const elements = {
  serviceState: document.querySelector("#serviceState"),
  serviceStateText: document.querySelector("#serviceStateText"),
  scanButton: document.querySelector("#scanButton"),
  pauseButton: document.querySelector("#pauseButton"),
  pauseButtonText: document.querySelector("#pauseButtonText"),
  pauseButtonIcon: document.querySelector("#pauseButtonIcon"),
  updateButton: document.querySelector("#updateButton"),
  optOutButton: document.querySelector("#optOutButton"),
  scanMeta: document.querySelector("#scanMeta"),
  metricOpen: document.querySelector("#metricOpen"),
  metricFailing: document.querySelector("#metricFailing"),
  metricPending: document.querySelector("#metricPending"),
  metricAttention: document.querySelector("#metricAttention"),
  notice: document.querySelector("#notice"),
  noticeTitle: document.querySelector("#noticeTitle"),
  noticeBody: document.querySelector("#noticeBody"),
  storageBand: document.querySelector("#storageBand"),
  storageHealth: document.querySelector("#storageHealth"),
  storageDatabase: document.querySelector("#storageDatabase"),
  storageReclaimable: document.querySelector("#storageReclaimable"),
  storageFree: document.querySelector("#storageFree"),
  storageTotal: document.querySelector("#storageTotal"),
  storageProgress: document.querySelector("#storageProgress"),
  loadingState: document.querySelector("#loadingState"),
  prView: document.querySelector("#prView"),
  outboxView: document.querySelector("#outboxView"),
  prRows: document.querySelector("#prRows"),
  outboxRows: document.querySelector("#outboxRows"),
  outboxLimitNotice: document.querySelector("#outboxLimitNotice"),
  prTabCount: document.querySelector("#prTabCount"),
  outboxTabCount: document.querySelector("#outboxTabCount"),
  outboxCountAll: document.querySelector("#outboxCountAll"),
  outboxCountPending: document.querySelector("#outboxCountPending"),
  outboxCountAttention: document.querySelector("#outboxCountAttention"),
  outboxCountSent: document.querySelector("#outboxCountSent"),
  outboxCountSuppressed: document.querySelector("#outboxCountSuppressed"),
  searchInput: document.querySelector("#searchInput"),
  prFilters: document.querySelector("#prFilters"),
  outboxFilters: document.querySelector("#outboxFilters"),
  prDialog: document.querySelector("#prDialog"),
  prDialogTitle: document.querySelector("#prDialogTitle"),
  prDialogBody: document.querySelector("#prDialogBody"),
  prDialogLink: document.querySelector("#prDialogLink"),
  outboxDialog: document.querySelector("#outboxDialog"),
  outboxDialogTitle: document.querySelector("#outboxDialogTitle"),
  outboxDialogBody: document.querySelector("#outboxDialogBody"),
  outboxDialogRetry: document.querySelector("#outboxDialogRetry"),
  optOutDialog: document.querySelector("#optOutDialog"),
  optOutInput: document.querySelector("#optOutInput"),
  addOptOutButton: document.querySelector("#addOptOutButton"),
  candidateList: document.querySelector("#candidateList"),
  optOutList: document.querySelector("#optOutList"),
  optOutSummary: document.querySelector("#optOutSummary"),
  retryDialog: document.querySelector("#retryDialog"),
  retryDialogText: document.querySelector("#retryDialogText"),
  confirmRetryButton: document.querySelector("#confirmRetryButton"),
  actionDialog: document.querySelector("#actionDialog"),
  actionDialogEyebrow: document.querySelector("#actionDialogEyebrow"),
  actionDialogTitle: document.querySelector("#actionDialogTitle"),
  actionDialogText: document.querySelector("#actionDialogText"),
  confirmActionButton: document.querySelector("#confirmActionButton"),
  toast: document.querySelector("#toast"),
};

const statusNames = {
  pending: "待发送",
  sent: "已发送",
  failed: "等待重试",
  dead: "重试耗尽",
  uncertain: "结果不确定",
  unmapped: "未映射",
  suppressed: "已跳过",
};

const pendingStatuses = new Set(["pending", "failed"]);
const attentionStatuses = new Set(["dead", "uncertain", "unmapped"]);
const retryableStatuses = new Set(["failed", "dead", "uncertain", "unmapped"]);

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
elements.pauseButton.addEventListener("click", togglePause);
elements.updateButton.addEventListener("click", openUpdateDialog);
elements.optOutButton.addEventListener("click", openOptOutDialog);
elements.optOutInput.addEventListener("input", renderOptOutManager);
elements.addOptOutButton.addEventListener("click", () => {
  const employeeNumber = normalizeEmployeeNumber(elements.optOutInput.value);
  if (employeeNumber) openAddOptOutDialog(employeeNumber);
});
elements.confirmRetryButton.addEventListener("click", confirmRetry);
elements.confirmActionButton.addEventListener("click", confirmAction);
elements.outboxDialogRetry.addEventListener("click", () => {
  const id = state.detailId;
  elements.outboxDialog.close();
  if (id) openRetryDialog(id);
});

document.addEventListener("click", (event) => {
  const closeButton = event.target.closest("[data-close-dialog]");
  if (closeButton) document.querySelector(`#${closeButton.dataset.closeDialog}`).close();

  const prButton = event.target.closest("[data-pr-detail]");
  if (prButton) openPrDialog(Number(prButton.dataset.prDetail));

  const outboxButton = event.target.closest("[data-outbox-detail]");
  if (outboxButton) openOutboxDialog(Number(outboxButton.dataset.outboxDetail));

  const retryButton = event.target.closest("[data-retry]");
  if (retryButton) openRetryDialog(Number(retryButton.dataset.retry));

  const addButton = event.target.closest("[data-add-opt-out]");
  if (addButton) openAddOptOutDialog(addButton.dataset.addOptOut);

  const removeButton = event.target.closest("[data-remove-opt-out]");
  if (removeButton) openRemoveOptOutDialog(removeButton.dataset.removeOptOut);
});

[
  elements.prDialog,
  elements.outboxDialog,
  elements.optOutDialog,
  elements.retryDialog,
  elements.actionDialog,
].forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

loadDashboard();
window.setInterval(() => loadDashboard(true), 5000);

async function loadDashboard(silent = false) {
  try {
    const params = new URLSearchParams({ outbox_limit: "500" });
    const statusQueries = {
      pending: "pending,failed",
      attention: "dead,uncertain,unmapped",
      sent: "sent",
      suppressed: "suppressed",
    };
    if (statusQueries[state.outboxFilter]) {
      params.set("outbox_status", statusQueries[state.outboxFilter]);
    }
    const response = await fetch(`/api/dashboard?${params}`, { cache: "no-store" });
    if (!response.ok) throw new Error(await responseError(response));
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
  const counts = data.outbox_counts || data.metrics.outbox_counts || {};
  elements.loadingState.hidden = true;
  elements.metricOpen.textContent = formatNumber(data.metrics.open_prs);
  elements.metricFailing.textContent = formatNumber(data.metrics.failing_prs);
  elements.metricPending.textContent = formatNumber(data.metrics.pending_delivery);
  elements.metricAttention.textContent = formatNumber(data.metrics.delivery_attention);
  elements.prTabCount.textContent = formatNumber(data.pull_requests.length);
  elements.outboxTabCount.textContent = formatNumber(counts.total);
  elements.outboxCountAll.textContent = formatNumber(counts.total);
  elements.outboxCountPending.textContent = formatNumber((counts.pending || 0) + (counts.failed || 0));
  elements.outboxCountAttention.textContent = formatNumber((counts.dead || 0) + (counts.uncertain || 0) + (counts.unmapped || 0));
  elements.outboxCountSent.textContent = formatNumber(counts.sent);
  elements.outboxCountSuppressed.textContent = formatNumber(counts.suppressed);
  renderHealth(data);
  renderStorage(data.storage || {});
  renderCurrentView();
  if (elements.optOutDialog.open) renderOptOutManager();
  if (elements.outboxDialog.open && state.detailId) openOutboxDialog(state.detailId, true);
}

function renderHealth(data) {
  const scan = data.scan;
  const runtime = data.runtime || {};
  const updating = ["requested", "updating"].includes(runtime.update_status);
  elements.scanButton.classList.toggle("scanning", Boolean(runtime.scanning));
  elements.scanButton.disabled = Boolean(runtime.scanning || runtime.scan_requested || runtime.paused || runtime.pause_requested || updating);
  elements.scanButton.querySelector("span").textContent = runtime.scanning
    ? "扫描中"
    : runtime.scan_requested
      ? "已安排"
      : "立即扫描";

  elements.pauseButton.disabled = updating;
  elements.pauseButtonText.textContent = runtime.paused ? "恢复监控" : runtime.pause_requested ? "等待暂停" : "暂停监控";
  elements.pauseButtonIcon.setAttribute("href", runtime.paused ? "#icon-play" : "#icon-pause");
  elements.updateButton.disabled = updating;

  if (updating) {
    setServiceState("scanning", runtime.update_status === "updating" ? "正在更新" : "等待更新");
  } else if (runtime.paused) {
    setServiceState("warning", "监控已暂停");
  } else if (runtime.pause_requested) {
    setServiceState("warning", "本轮后暂停");
  } else if (runtime.scanning) {
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
  const runtime = data.runtime || {};
  const scan = data.scan;
  if (runtime.update_status === "failed") {
    return { tone: "danger", title: "程序更新失败", body: runtime.update_message || "请查看运行日志" };
  }
  if (runtime.update_status === "current") {
    return { tone: "info", title: "程序已经是最新版本", body: runtime.update_message || "无需重启" };
  }
  if (scan && scan.errors.length) {
    return { tone: "danger", title: `最近扫描有 ${scan.errors.length} 个错误`, body: scan.errors[0] };
  }
  if (!runtime.paused && scan && isStale(scan.completed_at)) {
    return { tone: "warning", title: "监控超过 7 分钟没有完成扫描", body: "请检查进程、Gitea 网络和凭据状态。" };
  }
  if (data.storage && data.storage.warning) {
    return { tone: "warning", title: "本地磁盘空间不足", body: `当前剩余 ${formatBytes(data.storage.disk.free_bytes)}` };
  }
  if (data.metrics.delivery_attention) {
    return { tone: "warning", title: `${data.metrics.delivery_attention} 条消息需要处理`, body: "打开消息发送查看完整错误。" };
  }
  return null;
}

function renderStorage(storage) {
  const disk = storage.disk || {};
  const sqlite = storage.sqlite || {};
  const totalBytes = Number(disk.total_bytes || storage.disk_total_bytes || 0);
  const freeBytes = Number(disk.free_bytes || storage.free_bytes || 0);
  const usedPercent = totalBytes ? Math.max(0, Math.min(100, ((totalBytes - freeBytes) / totalBytes) * 100)) : 0;
  elements.storageDatabase.textContent = formatBytes(storage.total_bytes || storage.database_bytes);
  elements.storageReclaimable.textContent = formatBytes(storage.reclaimable_bytes || sqlite.reclaimable_bytes);
  elements.storageFree.textContent = formatBytes(freeBytes);
  elements.storageTotal.textContent = formatBytes(totalBytes);
  elements.storageProgress.value = usedPercent;
  elements.storageBand.dataset.tone = storage.warning_level || "ok";
  elements.storageHealth.textContent = storage.warning ? "空间偏低" : "空间充足";
}

function setView(view) {
  if (state.view === view) return;
  state.view = view;
  state.query = "";
  elements.searchInput.value = "";
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
  if (key === "outboxFilter") loadDashboard(true);
  else renderCurrentView();
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
    items = items.filter((item) => [item.number, item.title, item.author, item.author_w3, item.head_sha].join(" ").toLowerCase().includes(state.query));
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
      <div class="cell"><span class="cell-label">提交人</span><strong>${escapeHtml(item.author)}</strong>${item.author_w3 ? `<div class="subline mono">${escapeHtml(item.author_w3)}</div>` : ""}</div>
      <div class="cell"><span class="cell-label">当前命令</span><span class="mono">${escapeHtml(item.latest_ci_command || "--")}</span><div class="subline">${item.latest_ci_command_at ? relativeTime(item.latest_ci_command_at) : "暂无命令"}</div></div>
      <div class="cell">${status}</div>
      <div class="cell time-cell"><span class="cell-label">最近扫描</span>${relativeTime(item.scanned_at)}</div>
    </article>`;
  }).join("");
}

function renderOutbox() {
  const outboxQuery = state.data.outbox_query || {};
  elements.outboxLimitNotice.hidden = !outboxQuery.truncated;
  if (outboxQuery.truncated) {
    elements.outboxLimitNotice.textContent = `显示最近 ${outboxQuery.returned} 条，共 ${outboxQuery.available} 条`;
  }
  let items = state.data.outbox;
  if (state.outboxFilter === "pending") items = items.filter((item) => pendingStatuses.has(item.status));
  if (state.outboxFilter === "attention") items = items.filter((item) => attentionStatuses.has(item.status));
  if (state.outboxFilter === "sent") items = items.filter((item) => item.status === "sent");
  if (state.outboxFilter === "suppressed") items = items.filter((item) => item.status === "suppressed");
  if (state.query) {
    items = items.filter((item) => [item.pr_number, item.author, item.receiver, item.recipient_employee_number, item.message].join(" ").toLowerCase().includes(state.query));
  }
  if (!items.length) {
    const title = state.outboxFilter === "sent" ? "还没有已发送消息" : "没有匹配的发送记录";
    elements.outboxRows.innerHTML = emptyMarkup("inbox", title, "发送状态会在这里持续更新");
    return;
  }
  elements.outboxRows.innerHTML = items.map((item) => {
    const retryable = retryableStatuses.has(item.status);
    const receiver = item.receiver || (item.recipient_employee_number ? `工号 ${item.recipient_employee_number}` : item.author || "--");
    return `<article class="data-row outbox-grid">
      <div class="cell primary-cell"><button class="row-detail-button message-preview" type="button" data-outbox-detail="${item.id}"><strong>#${item.id} · PR #${item.pr_number}</strong><span>${escapeHtml(oneLine(item.message))}</span></button></div>
      <div class="cell"><span class="cell-label">收件人</span><strong>${escapeHtml(receiver)}</strong>${item.recipient_employee_number ? `<div class="subline mono">${escapeHtml(item.recipient_employee_number)}</div>` : ""}</div>
      <div class="cell"><span class="status-badge ${escapeAttribute(item.status)}">${escapeHtml(statusNames[item.status] || item.status)}</span>${item.last_error ? `<div class="subline">${escapeHtml(oneLine(item.last_error))}</div>` : ""}</div>
      <div class="cell mono"><span class="cell-label">尝试</span>${item.attempts}</div>
      <div class="cell time-cell"><span class="cell-label">更新时间</span>${relativeTime(item.updated_at)}</div>
      <div class="cell row-action"><button class="icon-button" type="button" data-outbox-detail="${item.id}" aria-label="查看消息 #${item.id}" title="查看完整记录"><svg class="icon" aria-hidden="true"><use href="#icon-eye"></use></svg></button>${retryable ? `<button class="icon-button" type="button" data-retry="${item.id}" aria-label="重试消息 #${item.id}" title="重新加入发送队列"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg></button>` : ""}</div>
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
    : `<div class="empty-state compact-empty"><svg class="icon" aria-hidden="true"><use href="#icon-check"></use></svg><strong>未发现关键失败</strong></div>`;
  elements.prDialogBody.innerHTML = `<div class="dialog-meta"><span>提交人 ${escapeHtml(item.author)}</span>${item.author_w3 ? `<span class="mono">${escapeHtml(item.author_w3)}</span>` : ""}<span class="mono">Head ${escapeHtml(item.head_sha.slice(0, 12))}</span><span>${escapeHtml(item.latest_ci_command || "暂无 CI 命令")}</span></div>${failures}`;
  elements.prDialog.showModal();
}

function openOutboxDialog(id, refresh = false) {
  const item = state.data.outbox.find((record) => record.id === id);
  if (!item) {
    if (!refresh) showToast("这条发送记录不在当前列表中", true);
    return;
  }
  state.detailId = id;
  elements.outboxDialogTitle.textContent = `消息 #${item.id} · PR #${item.pr_number}`;
  elements.outboxDialogBody.innerHTML = `<dl class="record-details">
    <div><dt>状态</dt><dd><span class="status-badge ${escapeAttribute(item.status)}">${escapeHtml(statusNames[item.status] || item.status)}</span></dd></div>
    <div><dt>收件人</dt><dd>${escapeHtml(item.receiver || "--")}</dd></div>
    <div><dt>工号</dt><dd class="mono">${escapeHtml(item.recipient_employee_number || "--")}</dd></div>
    <div><dt>尝试次数</dt><dd>${item.attempts}</dd></div>
    <div><dt>创建时间</dt><dd>${formatDateTime(item.created_at)}</dd></div>
    <div><dt>更新时间</dt><dd>${formatDateTime(item.updated_at)}</dd></div>
  </dl>
  <section class="record-block"><h3>发送内容</h3><pre>${escapeHtml(item.message)}</pre></section>
  ${item.last_error ? `<section class="record-block error-block"><h3>发送错误</h3><pre>${escapeHtml(item.last_error)}</pre></section>` : ""}`;
  elements.outboxDialogRetry.hidden = !retryableStatuses.has(item.status);
  if (!refresh) elements.outboxDialog.showModal();
}

function openRetryDialog(id) {
  const item = state.data.outbox.find((record) => record.id === id);
  if (!item) return;
  state.retryId = id;
  elements.retryDialogText.textContent = item.status === "uncertain"
    ? `消息 #${id} 可能已经送达，再次发送可能产生重复消息。`
    : `消息 #${id} 将清空失败计数并重新发送。`;
  elements.retryDialog.showModal();
}

async function confirmRetry() {
  if (!state.retryId) return;
  elements.confirmRetryButton.disabled = true;
  try {
    const response = await postJson(`/api/outbox/${state.retryId}/retry`, { confirm: true });
    if (!response.ok) throw new Error(await responseError(response));
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

function openOptOutDialog() {
  elements.optOutInput.value = "";
  renderOptOutManager();
  elements.optOutDialog.showModal();
}

function renderOptOutManager() {
  if (!state.data) return;
  const input = elements.optOutInput.value.trim().toLowerCase();
  const normalized = normalizeEmployeeNumber(input);
  const optedOut = new Set((state.data.opt_outs || []).map((item) => item.employee_number));
  const candidates = (state.data.recipient_candidates || []).filter((item) => {
    if (!item.employee_number || optedOut.has(item.employee_number)) return false;
    if (!input) return true;
    return [item.author, item.author_w3, item.employee_number].join(" ").toLowerCase().includes(input);
  });
  elements.addOptOutButton.disabled = !normalized || optedOut.has(normalized);
  elements.candidateList.innerHTML = candidates.length
    ? candidates.slice(0, 8).map((item) => `<button class="candidate-row" type="button" data-add-opt-out="${escapeAttribute(item.employee_number)}"><span><strong>${escapeHtml(item.author)}</strong><small>${escapeHtml(item.author_w3 || item.employee_number)} · ${item.open_prs} 个开放 PR</small></span><svg class="icon" aria-hidden="true"><use href="#icon-plus"></use></svg></button>`).join("")
    : "";

  const optOuts = state.data.opt_outs || [];
  elements.optOutSummary.textContent = `${optOuts.length} 人`;
  elements.optOutList.innerHTML = optOuts.length
    ? optOuts.map((item) => {
      const candidate = (state.data.recipient_candidates || []).find((value) => value.employee_number === item.employee_number);
      return `<div class="opt-out-row"><div><strong class="mono">${escapeHtml(item.employee_number)}</strong>${candidate ? `<span>${escapeHtml(candidate.author)} · ${escapeHtml(candidate.author_w3)}</span>` : `<span>WeLink 工号</span>`}</div><button class="icon-button" type="button" data-remove-opt-out="${escapeAttribute(item.employee_number)}" aria-label="移除工号 ${escapeAttribute(item.employee_number)}" title="移出免打扰"><svg class="icon" aria-hidden="true"><use href="#icon-trash"></use></svg></button></div>`;
    }).join("")
    : emptyMarkup("inbox", "名单为空", "所有提交人都会正常收到消息", true);
}

function openAddOptOutDialog(employeeNumber) {
  state.action = {
    endpoint: "/api/opt-outs/add",
    body: { employee_number: employeeNumber, confirm: true },
    success: `工号 ${employeeNumber} 已加入免打扰`,
    refreshOptOut: true,
  };
  openActionDialog("消息设置", "加入免打扰名单", `工号 ${employeeNumber} 的待发消息将被跳过。`, "确认加入", false);
}

function openRemoveOptOutDialog(employeeNumber) {
  state.action = {
    endpoint: "/api/opt-outs/remove",
    body: { employee_number: employeeNumber, confirm: true },
    success: `工号 ${employeeNumber} 已移出免打扰`,
    refreshOptOut: true,
  };
  openActionDialog("消息设置", "移出免打扰名单", `工号 ${employeeNumber} 将从下一条新消息开始恢复接收。`, "确认移除", true);
}

function togglePause() {
  const runtime = state.data && state.data.runtime ? state.data.runtime : {};
  if (runtime.paused) {
    runSimpleAction("/api/resume", { confirm: true }, "监控已恢复");
    return;
  }
  state.action = {
    endpoint: "/api/pause",
    body: { confirm: true },
    success: "已安排暂停监控",
  };
  openActionDialog("运行控制", "暂停监控", "如果正在扫描，本轮完成后暂停；工作台会继续运行。", "确认暂停", true);
}

function openUpdateDialog() {
  state.action = {
    endpoint: "/api/update",
    body: { confirm: true },
    success: "已安排更新程序",
  };
  openActionDialog("程序更新", "更新并重启", "仅在 main 工作区干净且可以安全快进时更新。", "确认更新", false);
}

function openActionDialog(eyebrow, title, text, buttonText, danger) {
  elements.actionDialogEyebrow.textContent = eyebrow;
  elements.actionDialogTitle.textContent = title;
  elements.actionDialogText.textContent = text;
  elements.confirmActionButton.textContent = buttonText;
  elements.confirmActionButton.classList.toggle("danger-button", danger);
  elements.confirmActionButton.classList.toggle("primary-button", !danger);
  elements.actionDialog.showModal();
}

async function confirmAction() {
  if (!state.action) return;
  const action = state.action;
  elements.confirmActionButton.disabled = true;
  try {
    const response = await postJson(action.endpoint, action.body);
    if (!response.ok) throw new Error(await responseError(response));
    elements.actionDialog.close();
    showToast(action.success);
    state.action = null;
    await loadDashboard(true);
    if (action.refreshOptOut && elements.optOutDialog.open) {
      elements.optOutInput.value = "";
      renderOptOutManager();
    }
  } catch (error) {
    showToast(String(error), true);
  } finally {
    elements.confirmActionButton.disabled = false;
  }
}

async function runSimpleAction(endpoint, body, success) {
  try {
    const response = await postJson(endpoint, body);
    if (!response.ok) throw new Error(await responseError(response));
    showToast(success);
    await loadDashboard(true);
  } catch (error) {
    showToast(String(error), true);
  }
}

async function requestScan() {
  elements.scanButton.disabled = true;
  try {
    const response = await postJson("/api/scan", {});
    if (!response.ok) throw new Error(await responseError(response));
    const payload = await response.json();
    showToast(payload.accepted ? "已安排立即扫描" : "当前无法安排扫描");
    await loadDashboard(true);
  } catch (error) {
    elements.scanButton.disabled = false;
    showToast(`无法安排扫描：${error}`, true);
  }
}

function postJson(path, body) {
  return fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Monitor-Action": "1" },
    body: JSON.stringify(body),
  });
}

async function responseError(response) {
  try {
    const payload = await response.json();
    return payload.error || `HTTP ${response.status}`;
  } catch (_) {
    return `HTTP ${response.status}`;
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
  state.toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 2800);
}

function emptyMarkup(icon, title, body, compact = false) {
  return `<div class="empty-state ${compact ? "compact-empty" : ""}"><svg class="icon" aria-hidden="true"><use href="#icon-${icon}"></use></svg><strong>${escapeHtml(title)}</strong><span>${escapeHtml(body)}</span></div>`;
}

function normalizeEmployeeNumber(value) {
  const match = String(value || "").trim().match(/^(?:[a-zA-Z])?(\d{8})$/);
  return match ? match[1] : "";
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

function formatBytes(value) {
  let bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes < 0) bytes = 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) {
    bytes /= 1024;
    index += 1;
  }
  const digits = index === 0 || bytes >= 100 ? 0 : bytes >= 10 ? 1 : 2;
  return `${bytes.toFixed(digits)} ${units[index]}`;
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "--";
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date);
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
