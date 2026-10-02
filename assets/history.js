import { asArray, buildPublicRecordUrl, publicRecordServiceIds, safeText } from "./status-portal-core.js";

const API_PATH = "/api/status-history.json";
const DAY_MS = 24 * 60 * 60 * 1000;
const ALLOWED_DAYS = new Set([7, 14, 30]);
const STATUS_COPY = {
  operational: "稼働中",
  maintenance: "メンテナンス",
  degraded: "一部影響",
  outage: "停止",
  unknown: "確認中",
};

const elements = {
  refreshButton: document.querySelector("#refreshButton"),
  rangeButtons: [...document.querySelectorAll("[data-days]")],
  rangeText: document.querySelector("#rangeText"),
  serviceCount: document.querySelector("#serviceCount"),
  impactDayCount: document.querySelector("#impactDayCount"),
  generatedAt: document.querySelector("#generatedAt"),
  historyServiceList: document.querySelector("#historyServiceList"),
  historyIncidentList: document.querySelector("#historyIncidentList"),
  footerTimestamp: document.querySelector("#footerTimestamp"),
  toast: document.querySelector("#toast"),
};

const params = new URLSearchParams(window.location.search);
const initialDays = Number(params.get("days"));
let selectedDays = ALLOWED_DAYS.has(initialDays) ? initialDays : 30;
let toastTimer = null;
let latestHistory = null;
let activeDayButton = null;

function formatDate(value, options = {}) {
  const date = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(date.getTime())) return "--";
  return new Intl.DateTimeFormat("ja-JP", {
    month: "short",
    day: "numeric",
    timeZone: "UTC",
    ...options,
  }).format(date);
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return new Intl.DateTimeFormat("ja-JP", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Tokyo",
  }).format(date);
}

function formatAvailability(value) {
  return typeof value === "number" ? `${value.toFixed(value % 1 === 0 ? 0 : 2)}%` : "--";
}

function showToast(message) {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.add("visible");
  toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 2600);
}

function setLoading(loading) {
  elements.refreshButton.disabled = loading;
  elements.refreshButton.classList.toggle("loading", loading);
  elements.refreshButton.setAttribute("aria-busy", String(loading));
}

function updateRangeButtons() {
  for (const button of elements.rangeButtons) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.days) === selectedDays));
  }
}

function createHistoryDay(day, service) {
  const cell = document.createElement("button");
  const status = STATUS_COPY[day.status] ? day.status : "unknown";
  const availability = formatAvailability(day.availability_percent);
  cell.type = "button";
  cell.className = "history-day";
  cell.dataset.status = status;
  cell.textContent = new Date(`${day.date}T00:00:00Z`).getUTCDate();
  cell.setAttribute("aria-label", `${service.name} ${formatDate(day.date, { year: "numeric" })}：${STATUS_COPY[status]} / 稼働率 ${availability}`);
  cell.setAttribute("aria-haspopup", "dialog");
  cell.setAttribute("aria-expanded", "false");
  cell.addEventListener("click", (event) => {
    event.stopPropagation();
    openDayPopover(cell, day, service);
  });
  cell.addEventListener("pointerenter", (event) => {
    if (event.pointerType === "mouse") openDayPopover(cell, day, service);
  });
  return cell;
}

function textElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = text;
  return element;
}

function metaRow(label, value) {
  const row = document.createElement("div");
  row.className = "portal-meta-row";
  row.append(textElement("span", "", label), textElement("strong", "", value));
  return row;
}

function recordRange(record) {
  const start = new Date(record.started_at || record.starts_at || record.published_at || 0).getTime();
  const endValue = record.resolved_at || record.ends_at;
  const end = endValue ? new Date(endValue).getTime() : Date.now();
  return [start, end];
}

// The API aggregates days in range.timezone (Asia/Tokyo); older responses without it used UTC.
function historyIsJst() {
  return latestHistory?.range?.timezone === "Asia/Tokyo";
}

// Public incidents / maintenance that affected this service on this calendar day.
function relatedRecords(day, service) {
  const dayStart = new Date(`${day.date}T00:00:00${historyIsJst() ? "+09:00" : "Z"}`).getTime();
  const dayEnd = dayStart + DAY_MS;
  return [...asArray(latestHistory?.incidents), ...asArray(latestHistory?.maintenance)].filter((record) => {
    if (!publicRecordServiceIds(record).includes(service.id)) return false;
    const [start, end] = recordRange(record);
    return start > 0 && start < dayEnd && end >= dayStart;
  });
}

function ensureDayPopover() {
  let popover = document.querySelector("#historyDayPopover");
  if (popover) return popover;
  popover = document.createElement("div");
  popover.id = "historyDayPopover";
  popover.className = "timeline-popover";
  popover.setAttribute("role", "dialog");
  popover.setAttribute("aria-label", "日別の稼働詳細");
  popover.hidden = true;
  popover.tabIndex = -1;
  document.body.append(popover);
  return popover;
}

function positionDayPopover() {
  const popover = document.querySelector("#historyDayPopover");
  if (!popover || popover.hidden || !activeDayButton) return;
  const rect = activeDayButton.getBoundingClientRect();
  const margin = 12;
  const width = Math.min(320, window.innerWidth - margin * 2);
  popover.style.width = `${width}px`;
  popover.style.left = `${Math.max(margin, Math.min(window.innerWidth - width - margin, rect.left + rect.width / 2 - width / 2))}px`;
  const below = rect.bottom + 8;
  const fitsBelow = below + popover.offsetHeight + margin <= window.innerHeight;
  popover.style.top = `${fitsBelow ? below : Math.max(margin, rect.top - popover.offsetHeight - 8)}px`;
}

function emptyDayNote(status) {
  if (status === "unknown") return "この日の監視データはありません";
  if (status === "operational") return "この日に問題は観測されていません";
  return "この日に関連する公開障害情報はありません";
}

function openDayPopover(button, day, service) {
  const popover = ensureDayPopover();
  if (activeDayButton && activeDayButton !== button) activeDayButton.setAttribute("aria-expanded", "false");
  activeDayButton = button;
  button.setAttribute("aria-expanded", "true");
  const status = STATUS_COPY[day.status] ? day.status : "unknown";

  const head = document.createElement("div");
  head.className = "timeline-popover-head";
  const copy = document.createElement("div");
  copy.append(
    textElement("span", "portal-record-type", service.name),
    textElement("strong", "", `${formatDate(day.date, { year: "numeric" })}・${STATUS_COPY[status]}`),
  );
  const close = textElement("button", "timeline-popover-close", "閉じる");
  close.type = "button";
  close.addEventListener("click", () => closeDayPopover({ restoreFocus: true }));
  head.append(copy, close);

  const meta = document.createElement("div");
  meta.className = "portal-record-meta";
  meta.append(
    metaRow("稼働率", formatAvailability(day.availability_percent)),
    metaRow("観測", `${Number(day.samples || 0).toLocaleString("ja-JP")}件`),
  );
  popover.replaceChildren(head, meta);

  const records = relatedRecords(day, service);
  if (!records.length) popover.append(textElement("p", "timeline-popover-empty", emptyDayNote(status)));
  for (const record of records) {
    const detail = document.createElement("div");
    detail.className = "timeline-popover-incident";
    detail.append(
      textElement("strong", "", safeText(record.title, "公開情報")),
      textElement("p", "", safeText(record.summary, "公開された詳細情報はありません。")),
    );
    const url = buildPublicRecordUrl(record, { origin: window.location.origin, history: true });
    if (url) {
      const link = textElement("a", "portal-detail-link", "詳細を見る");
      link.href = url;
      detail.append(link);
    }
    popover.append(detail);
  }
  popover.append(textElement("p", "timeline-popover-time", historyIsJst() ? "日付は日本時間で集計しています" : "日付はUTC基準で集計しています"));
  popover.hidden = false;
  positionDayPopover();
}

function closeDayPopover({ restoreFocus = false } = {}) {
  const popover = document.querySelector("#historyDayPopover");
  if (!popover || popover.hidden) return;
  popover.hidden = true;
  popover.replaceChildren();
  const button = activeDayButton;
  activeDayButton = null;
  button?.setAttribute("aria-expanded", "false");
  if (restoreFocus && button?.isConnected) button.focus({ preventScroll: true });
}

function createServiceCard(service, range) {
  const card = document.createElement("article");
  card.className = "history-card";

  const head = document.createElement("div");
  head.className = "history-card-head";

  const titleWrap = document.createElement("div");
  titleWrap.className = "history-service-title";
  const mark = document.createElement("span");
  mark.className = "history-service-mark";
  mark.textContent = service.name.slice(0, 1).toUpperCase();
  mark.setAttribute("aria-hidden", "true");
  const titleCopy = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = service.name;
  const description = document.createElement("p");
  description.textContent = `${service.group} · ${service.description}`;
  titleCopy.append(title, description);
  titleWrap.append(mark, titleCopy);

  const metrics = document.createElement("div");
  metrics.className = "history-service-metrics";
  const availabilityMetric = document.createElement("div");
  const availabilityLabel = document.createElement("span");
  availabilityLabel.textContent = "期間内稼働率";
  const availabilityValue = document.createElement("strong");
  availabilityValue.textContent = formatAvailability(service.availability_percent);
  availabilityMetric.append(availabilityLabel, availabilityValue);

  const currentMetric = document.createElement("div");
  const currentLabel = document.createElement("span");
  currentLabel.textContent = "現在の状態";
  const currentValue = document.createElement("strong");
  currentValue.className = "history-current";
  currentValue.dataset.status = STATUS_COPY[service.current_status] ? service.current_status : "unknown";
  currentValue.textContent = STATUS_COPY[currentValue.dataset.status];
  currentMetric.append(currentLabel, currentValue);
  metrics.append(availabilityMetric, currentMetric);
  head.append(titleWrap, metrics);

  const gridWrap = document.createElement("div");
  gridWrap.className = "history-grid-wrap";
  const grid = document.createElement("div");
  grid.className = "history-day-grid";
  grid.style.setProperty("--history-days", String(range.days));
  for (const day of service.days) grid.append(createHistoryDay(day, service));

  const axis = document.createElement("div");
  axis.className = "history-axis";
  axis.style.setProperty("--history-days", String(range.days));
  const from = document.createElement("span");
  from.textContent = formatDate(range.from_date);
  const to = document.createElement("span");
  to.textContent = `${formatDate(range.to_date)}（今日）`;
  axis.append(from, to);
  gridWrap.append(grid, axis);
  card.append(head, gridWrap);
  return card;
}

function renderIncidents(incidents) {
  elements.historyIncidentList.replaceChildren();
  if (!Array.isArray(incidents) || incidents.length === 0) {
    const state = document.createElement("div");
    state.className = "empty-state";
    const icon = document.createElement("span");
    icon.className = "empty-state-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = '<svg viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg>';
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = "公開中の障害記録はありません";
    const description = document.createElement("p");
    description.textContent = "現在、この期間に掲載されている障害・メンテナンス情報はありません。";
    copy.append(title, description);
    state.append(icon, copy);
    elements.historyIncidentList.append(state);
    return;
  }

  for (const incident of incidents) {
    const item = document.createElement("article");
    item.className = "incident-item";
    const title = document.createElement("strong");
    title.textContent = String(incident.title || "障害情報");
    const description = document.createElement("p");
    description.textContent = String(incident.message || incident.description || "詳細情報を確認しています。");
    item.append(title, description);
    elements.historyIncidentList.append(item);
  }
}

function renderHistory(data) {
  closeDayPopover();
  latestHistory = data;
  const range = data.range;
  const services = Array.isArray(data.services) ? data.services : [];
  elements.rangeText.textContent = `${formatDate(range.from_date)} – ${formatDate(range.to_date)}`;
  elements.serviceCount.textContent = String(services.length);
  elements.generatedAt.textContent = formatDateTime(data.generated_at);
  elements.footerTimestamp.textContent = `Updated ${formatDateTime(data.generated_at)}`;

  const impactDates = new Set();
  for (const service of services) {
    for (const day of service.days || []) {
      if (["maintenance", "degraded", "outage"].includes(day.status)) impactDates.add(day.date);
    }
  }
  elements.impactDayCount.textContent = `${impactDates.size}日`;

  elements.historyServiceList.replaceChildren();
  for (const service of services) {
    elements.historyServiceList.append(createServiceCard(service, range));
  }
  if (services.length === 0) {
    const empty = document.createElement("div");
    empty.className = "history-error";
    empty.innerHTML = "<strong>履歴データがありません</strong><p>監視サービスが登録されると、ここに履歴が表示されます。</p>";
    elements.historyServiceList.append(empty);
  }
  renderIncidents(data.incidents);
}

function renderError() {
  elements.historyServiceList.replaceChildren();
  const error = document.createElement("div");
  error.className = "history-error";
  const title = document.createElement("strong");
  title.textContent = "履歴を取得できませんでした";
  const description = document.createElement("p");
  description.textContent = "時間をおいて再度更新してください。現在の稼働状況はトップページから確認できます。";
  error.append(title, description);
  elements.historyServiceList.append(error);
}

async function loadHistory({ announce = false } = {}) {
  setLoading(true);
  updateRangeButtons();
  try {
    const response = await fetch(`${API_PATH}?days=${selectedDays}&t=${Date.now()}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`status history request failed: ${response.status}`);
    const data = await response.json();
    renderHistory(data);
    if (announce) showToast("稼働履歴を更新しました");
  } catch (error) {
    console.error(error);
    renderError();
    showToast("履歴の取得に失敗しました");
  } finally {
    setLoading(false);
  }
}

elements.refreshButton.addEventListener("click", () => loadHistory({ announce: true }));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeDayPopover({ restoreFocus: true });
});
document.addEventListener("pointerdown", (event) => {
  const popover = document.querySelector("#historyDayPopover");
  if (!popover || popover.hidden || popover.contains(event.target) || activeDayButton?.contains(event.target)) return;
  closeDayPopover();
});
// Mouse users get hover previews; close once the pointer leaves both the grid and the popover.
let hoverCloseTimer = null;
function scheduleHoverClose(event) {
  if (event.pointerType !== "mouse") return;
  window.clearTimeout(hoverCloseTimer);
  hoverCloseTimer = window.setTimeout(() => {
    const popover = document.querySelector("#historyDayPopover");
    if (popover?.matches(":hover") || activeDayButton?.matches(":hover") || popover?.contains(document.activeElement)) return;
    closeDayPopover();
  }, 160);
}
elements.historyServiceList.addEventListener("pointerout", scheduleHoverClose);
ensureDayPopover().addEventListener("pointerleave", scheduleHoverClose);
window.addEventListener("resize", positionDayPopover);
window.addEventListener("scroll", positionDayPopover, { passive: true });
for (const button of elements.rangeButtons) {
  button.addEventListener("click", () => {
    const days = Number(button.dataset.days);
    if (!ALLOWED_DAYS.has(days) || days === selectedDays) return;
    selectedDays = days;
    const url = new URL(window.location.href);
    url.searchParams.set("days", String(days));
    window.history.replaceState({}, "", url);
    loadHistory();
  });
}

updateRangeButtons();
loadHistory();
