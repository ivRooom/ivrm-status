import {
  asArray,
  buildPublicRecordUrl,
  combineArchiveRecords,
  filterArchiveRecords,
  parsePortalDeepLink,
  publicRecordServiceIds,
  publicRecordStatus,
  publicRecordType,
  safeText,
} from "./status-portal-core.js";

const API_PATH = "/api/status.json";
const $ = (id) => document.getElementById(id);

const STATUS_COPY = {
  investigating: "調査中",
  identified: "原因特定済み",
  monitoring: "復旧監視中",
  resolved: "復旧済み",
  scheduled: "予定",
  in_progress: "実施中",
  completed: "完了",
  cancelled: "中止",
  info: "お知らせ",
  warning: "重要なお知らせ",
};

let snapshot = null;
let records = [];
let deepLinkHandled = false;

function formatDateTime(value) {
  const date = new Date(value || 0);
  if (Number.isNaN(date.getTime())) return "--";
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function textElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = safeText(text);
  return node;
}

function showToast(message) {
  const toast = $("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 2600);
}

function clipboardFallback(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.className = "portal-clipboard-fallback";
  document.body.append(textarea);
  textarea.select();
  let copied = false;
  try { copied = document.execCommand("copy"); } catch { copied = false; }
  textarea.remove();
  return copied;
}

async function copyUrl(url) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(url);
    showToast("URLをコピーしました");
  } catch {
    showToast(clipboardFallback(url) ? "URLをコピーしました" : "コピーできませんでした。URLを選択してコピーしてください。");
  }
}

function createShareActions(record) {
  const actions = document.createElement("div");
  actions.className = "portal-share-actions";
  const url = buildPublicRecordUrl(record, { origin: window.location.origin, history: true });
  if (!url) return actions;

  const copy = textElement("button", "portal-action-button", "URLをコピー");
  copy.type = "button";
  copy.dataset.portalCopyUrl = url;
  copy.addEventListener("click", () => copyUrl(url));
  actions.append(copy);

  if (typeof navigator.share === "function") {
    const share = textElement("button", "portal-action-button", "共有");
    share.type = "button";
    share.addEventListener("click", async () => {
      try { await navigator.share({ title: safeText(record?.title, "ivRooom Status"), url }); }
      catch (error) { if (error?.name !== "AbortError") showToast("共有を開始できませんでした"); }
    });
    actions.append(share);
  }
  return actions;
}

function serviceNames(record) {
  const map = new Map(asArray(snapshot?.services).map((service) => [safeText(service?.id), safeText(service?.name, safeText(service?.id))]));
  const ids = publicRecordServiceIds(record);
  return ids.length ? ids.map((id) => map.get(id) || id).join("、") : "対象サービスなし";
}

function addMeta(container, label, value) {
  if (!safeText(value)) return;
  const row = document.createElement("div");
  row.className = "portal-meta-row";
  row.append(textElement("span", "", label), textElement("strong", "", value));
  container.append(row);
}

function renderRecord(record) {
  const type = publicRecordType(record);
  const card = document.createElement("article");
  card.className = `portal-record-card portal-${type}-card`;
  card.id = `public-${safeText(record.public_id)}`;
  card.tabIndex = -1;
  card.dataset.publicId = safeText(record.public_id);
  card.dataset.recordType = type;

  const head = document.createElement("div");
  head.className = "portal-record-head";
  const titleWrap = document.createElement("div");
  titleWrap.append(textElement("span", "portal-record-type", type?.toUpperCase()), textElement("h3", "", record.title || "公開情報"));
  const statusValue = publicRecordStatus(record);
  head.append(titleWrap, textElement("span", `portal-state portal-state-${statusValue}`, STATUS_COPY[statusValue] || statusValue));
  card.append(head);

  const body = type === "announcement" ? record.body : record.summary;
  card.append(textElement("p", `portal-record-summary${type === "announcement" ? " portal-preserve-lines" : ""}`, body || "公開された詳細情報はありません。"));

  const meta = document.createElement("div");
  meta.className = "portal-record-meta";
  addMeta(meta, "Public ID", record.public_id);
  addMeta(meta, "対象", serviceNames(record));
  if (type === "incident") {
    addMeta(meta, "発生", formatDateTime(record.started_at));
    if (record.resolved_at) addMeta(meta, "復旧", formatDateTime(record.resolved_at));
    addMeta(meta, "影響", safeText(record.impact, "不明"));
  } else if (type === "maintenance") {
    addMeta(meta, "開始", formatDateTime(record.starts_at));
    addMeta(meta, "終了", formatDateTime(record.ends_at));
  } else {
    addMeta(meta, "公開", formatDateTime(record.published_at));
    if (record.expires_at) addMeta(meta, "掲載期限", formatDateTime(record.expires_at));
  }
  card.append(meta);

  if (type === "incident" && asArray(record.updates).length) {
    const details = document.createElement("details");
    details.className = "portal-updates";
    details.append(textElement("summary", "", `更新履歴 ${record.updates.length}件`));
    const list = document.createElement("ol");
    for (const update of record.updates) {
      const item = document.createElement("li");
      item.append(textElement("span", "portal-update-meta", `${formatDateTime(update.published_at)} · ${STATUS_COPY[update.status] || safeText(update.status)}`), textElement("p", "", update.message));
      list.append(item);
    }
    details.append(list);
    card.append(details);
  }

  card.append(createShareActions(record));
  return card;
}

function currentFilters() {
  return {
    type: $("archiveTypeFilter")?.value || "all",
    status: $("archiveStatusFilter")?.value || "all",
    service: $("archiveServiceFilter")?.value || "all",
    fromDate: $("archiveFromDate")?.value || "",
    toDate: $("archiveToDate")?.value || "",
  };
}

function renderArchive() {
  const list = $("publicArchiveList");
  if (!list) return;
  const filtered = filterArchiveRecords(records, currentFilters());
  list.replaceChildren();
  $("archiveResultCount").textContent = `${filtered.length}件 / 全${records.length}件`;

  if (!filtered.length) {
    list.append(textElement("div", "portal-archive-empty", records.length ? "条件に一致する公開情報はありません。" : "公開済みのIncident / Maintenance / Announcementはありません。"));
    return;
  }
  for (const record of filtered) list.append(renderRecord(record));
}

function fillSelect(select, values, labelMap = new Map()) {
  if (!select) return;
  const current = select.value;
  const first = select.querySelector("option")?.cloneNode(true);
  select.replaceChildren();
  if (first) select.append(first);
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = labelMap.get(value) || STATUS_COPY[value] || value;
    select.append(option);
  }
  if ([...select.options].some((option) => option.value === current)) select.value = current;
}

function updateFilterOptions() {
  const statusValues = [...new Set(records.map(publicRecordStatus).filter(Boolean))].sort();
  fillSelect($("archiveStatusFilter"), statusValues);

  const serviceMap = new Map(asArray(snapshot?.services).map((service) => [safeText(service?.id), safeText(service?.name, safeText(service?.id))]));
  const serviceIds = [...new Set(records.flatMap(publicRecordServiceIds))].sort();
  fillSelect($("archiveServiceFilter"), serviceIds, serviceMap);
}

function handleDeepLink() {
  if (deepLinkHandled) return;
  deepLinkHandled = true;
  const deepLink = parsePortalDeepLink(window.location.search);
  if (!deepLink) return;
  if (!deepLink.valid) {
    showToast("共有URLのID形式が正しくないため、通常表示に戻しました");
    return;
  }
  const record = records.find((item) => safeText(item.public_id) === deepLink.id);
  if (!record) {
    showToast("指定された公開情報は見つかりませんでした");
    return;
  }
  $("archiveTypeFilter").value = publicRecordType(record);
  $("archiveStatusFilter").value = "all";
  $("archiveServiceFilter").value = "all";
  $("archiveFromDate").value = "";
  $("archiveToDate").value = "";
  renderArchive();
  const target = document.getElementById(`public-${deepLink.id}`);
  if (!target) return;
  target.classList.add("is-deep-linked");
  target.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
  target.focus({ preventScroll: true });
}

async function loadArchive({ announce = false } = {}) {
  try {
    const response = await fetch(`${API_PATH}?archive=${Date.now()}`, { cache: "no-store", headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!data || !Array.isArray(data.services)) throw new Error("invalid response");
    snapshot = data;
    records = combineArchiveRecords(data);
    updateFilterOptions();
    renderArchive();
    handleDeepLink();
    if (announce) showToast("公開履歴を更新しました");
  } catch (error) {
    console.error(error);
    const list = $("publicArchiveList");
    list?.replaceChildren(textElement("div", "portal-archive-empty", "公開履歴を取得できませんでした。現在のサービス稼働履歴は上部で確認できます。"));
    if (announce) showToast("公開履歴の取得に失敗しました");
  }
}

for (const id of ["archiveTypeFilter", "archiveStatusFilter", "archiveServiceFilter", "archiveFromDate", "archiveToDate"]) {
  $(id)?.addEventListener("change", renderArchive);
}
$("refreshButton")?.addEventListener("click", () => window.setTimeout(() => loadArchive({ announce: true }), 250));

loadArchive();