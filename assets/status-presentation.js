import {
  ANNOUNCEMENT_ID_PATTERN,
  INCIDENT_ID_PATTERN,
  MAINTENANCE_ID_PATTERN,
  asArray,
  buildPublicRecordUrl,
  normalizePublicCollections,
  normalizeTimelineDetails,
  parsePortalDeepLink,
  publicRecordServiceIds,
  safeText,
} from "./status-portal-core.js";

const $ = (id) => document.getElementById(id);
const REQUEST_TIMEOUT_MS = 8_000;

const IMPACT_COPY = {
  maintenance: {
    eyebrow: "SCHEDULED MAINTENANCE",
    title: "メンテナンスを実施しています",
    message: "一部サービスで予定されたメンテナンスを実施しています。",
  },
  degraded: {
    eyebrow: "PARTIAL SERVICE IMPACT",
    title: "一部サービスに影響があります",
    message: "サービスは利用できますが、一部機能で遅延や不安定な状態を確認しています。",
  },
  outage: {
    eyebrow: "SERVICE DISRUPTION",
    title: "サービス障害が発生しています",
    message: "現在、利用者影響のある障害を確認しています。復旧状況はこのページで更新します。",
  },
};

const IMPACT_PRIORITY = {
  maintenance: 1,
  degraded: 2,
  outage: 3,
};

const STATUS_COPY = {
  operational: "正常稼働",
  maintenance: "メンテナンス中",
  degraded: "一部影響あり",
  outage: "障害発生中",
  unknown: "確認中",
};

const INCIDENT_STATUS_COPY = {
  investigating: "調査中",
  identified: "原因特定済み",
  monitoring: "復旧監視中",
  resolved: "復旧済み",
};

const IMPACT_LABEL = {
  none: "影響なし",
  minor: "軽微",
  major: "大きな影響",
  critical: "重大",
};

const MAINTENANCE_STATE_COPY = {
  scheduled: "予定",
  in_progress: "実施中",
  completed: "完了",
  cancelled: "中止",
};

const ANNOUNCEMENT_KIND_COPY = {
  info: "お知らせ",
  warning: "重要なお知らせ",
};

const portalState = {
  snapshot: null,
  activePopoverAnchor: null,
  closeTimer: null,
  deepLinkHandled: false,
  restoringPopoverFocus: false,
  refreshing: false,
};

function ensurePortalStyles() {
  if (document.querySelector('link[data-status-portal-styles]')) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = "/assets/status-portal.css?v=20260830-1";
  link.dataset.statusPortalStyles = "true";
  document.head.append(link);
}

function serviceStateFromElement(element) {
  return ["operational", "maintenance", "degraded", "outage", "unknown"]
    .find((status) => element.classList.contains(status)) || "unknown";
}

function serviceNameFromElement(element) {
  return element.closest(".service-row")?.querySelector(".service-identity strong")?.textContent?.trim() || "サービス";
}

function applyHeroCopy(status, copy) {
  document.body.dataset.overallStatus = status;
  $("overallEyebrow").textContent = copy.eyebrow;
  $("overallTitle").textContent = copy.title;
  $("overallMessage").textContent = copy.message;
}

function updateStatusPresentation() {
  const stateElements = [...document.querySelectorAll(".service-state")];
  if (!stateElements.length) return;

  const services = stateElements.map((element) => ({
    name: serviceNameFromElement(element),
    status: serviceStateFromElement(element),
  }));

  const highestImpact = services
    .filter((service) => Object.hasOwn(IMPACT_PRIORITY, service.status))
    .sort((a, b) => IMPACT_PRIORITY[b.status] - IMPACT_PRIORITY[a.status])[0];

  if (highestImpact) {
    applyHeroCopy(highestImpact.status, IMPACT_COPY[highestImpact.status]);
    return;
  }

  const unknownServices = services.filter((service) => service.status === "unknown");
  if (!unknownServices.length) return;

  if (unknownServices.length === services.length) {
    applyHeroCopy("unknown", {
      eyebrow: "STATUS CHECK IN PROGRESS",
      title: "サービスの状態を確認しています",
      message: "監視対象は取得できていますが、最新の稼働データを待っています。",
    });
    return;
  }

  const names = unknownServices.slice(0, 2).map((service) => service.name).join("、");
  const suffix = unknownServices.length > 2 ? `ほか${unknownServices.length - 2}件` : "";
  applyHeroCopy("unknown", {
    eyebrow: "PARTIAL STATUS AVAILABLE",
    title: "一部サービスの状態を確認中です",
    message: `${names}${suffix}の最新状態を確認中です。取得済みのサービスに利用者影響は確認されていません。`,
  });
}

function formatDateTime(value) {
  const raw = safeText(value);
  if (!raw) return "--";
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return "--";
  return new Intl.DateTimeFormat("ja-JP", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatBucketRange(bucket) {
  return `${formatDateTime(bucket?.start_at)} – ${formatDateTime(bucket?.end_at)}`;
}

function createTextElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = safeText(text);
  return element;
}

function appendMetaRow(container, label, value) {
  if (!safeText(value)) return;
  const row = document.createElement("div");
  row.className = "portal-meta-row";
  row.append(createTextElement("span", "", label), createTextElement("strong", "", value));
  container.append(row);
}

function serviceNameMap(snapshot) {
  return new Map(asArray(snapshot?.services).map((service) => [safeText(service?.id), safeText(service?.name, safeText(service?.id))]));
}

function serviceListText(record, snapshot) {
  const names = serviceNameMap(snapshot);
  const ids = publicRecordServiceIds(record);
  if (!ids.length) return "対象サービスなし";
  return ids.map((id) => names.get(id) || id).join("、");
}

function showPortalToast(message) {
  const toast = $("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(showPortalToast.timer);
  showPortalToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 2600);
}

function clipboardFallback(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.className = "portal-clipboard-fallback";
  document.body.append(textarea);
  textarea.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  textarea.remove();
  return copied;
}

async function copyText(text, successMessage = "URLをコピーしました") {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(text);
    showPortalToast(successMessage);
    return true;
  } catch {
    const copied = clipboardFallback(text);
    showPortalToast(copied ? successMessage : "コピーできませんでした。URLを選択してコピーしてください。");
    return copied;
  }
}

function createShareActions(record, { history = false } = {}) {
  const actions = document.createElement("div");
  actions.className = "portal-share-actions";
  const url = buildPublicRecordUrl(record, { origin: window.location.origin, history });
  if (!url) return actions;

  const copyButton = createTextElement("button", "portal-action-button", "URLをコピー");
  copyButton.type = "button";
  copyButton.dataset.portalCopyUrl = url;
  copyButton.addEventListener("click", () => copyText(url));
  actions.append(copyButton);

  if (typeof navigator.share === "function") {
    const shareButton = createTextElement("button", "portal-action-button", "共有");
    shareButton.type = "button";
    shareButton.addEventListener("click", async () => {
      try {
        await navigator.share({ title: safeText(record?.title, "ivRooom Status"), url });
      } catch (error) {
        if (error?.name !== "AbortError") showPortalToast("共有を開始できませんでした");
      }
    });
    actions.append(shareButton);
  }
  return actions;
}

function createIncidentCard(incident, { compact = false, history = false } = {}) {
  const card = document.createElement("article");
  card.className = `portal-record-card portal-incident-card${compact ? " is-compact" : ""}`;
  card.id = `public-${safeText(incident.public_id)}`;
  card.tabIndex = -1;
  card.dataset.publicId = safeText(incident.public_id);
  card.dataset.recordType = "incident";

  const head = document.createElement("div");
  head.className = "portal-record-head";
  const titleWrap = document.createElement("div");
  const eyebrow = createTextElement("span", "portal-record-type", "INCIDENT");
  const title = createTextElement("h3", "", incident.title || "障害情報");
  titleWrap.append(eyebrow, title);
  const status = createTextElement("span", `portal-state portal-state-${safeText(incident.status, "unknown")}`, INCIDENT_STATUS_COPY[incident.status] || "状態確認中");
  head.append(titleWrap, status);
  card.append(head);

  const summary = createTextElement("p", "portal-record-summary", incident.summary || "公開された詳細情報はありません。");
  card.append(summary);

  const meta = document.createElement("div");
  meta.className = "portal-record-meta";
  appendMetaRow(meta, "Public ID", incident.public_id);
  appendMetaRow(meta, "影響", IMPACT_LABEL[incident.impact] || safeText(incident.impact, "不明"));
  appendMetaRow(meta, "対象", serviceListText(incident, portalState.snapshot));
  appendMetaRow(meta, "発生", formatDateTime(incident.started_at));
  if (incident.resolved_at) appendMetaRow(meta, "復旧", formatDateTime(incident.resolved_at));
  card.append(meta);

  if (!compact && asArray(incident.updates).length) {
    const details = document.createElement("details");
    details.className = "portal-updates";
    const summaryNode = createTextElement("summary", "", `更新履歴 ${incident.updates.length}件`);
    const list = document.createElement("ol");
    for (const update of incident.updates) {
      const item = document.createElement("li");
      const updateMeta = createTextElement("span", "portal-update-meta", `${formatDateTime(update.published_at)} · ${INCIDENT_STATUS_COPY[update.status] || safeText(update.status)}`);
      const message = createTextElement("p", "", update.message);
      item.append(updateMeta, message);
      list.append(item);
    }
    details.append(summaryNode, list);
    card.append(details);
  }

  card.append(createShareActions(incident, { history }));
  return card;
}

function createMaintenanceCard(maintenance, { compact = false, history = false } = {}) {
  const card = document.createElement("article");
  card.className = `portal-record-card portal-maintenance-card${compact ? " is-compact" : ""}`;
  card.id = `public-${safeText(maintenance.public_id)}`;
  card.tabIndex = -1;
  card.dataset.publicId = safeText(maintenance.public_id);
  card.dataset.recordType = "maintenance";

  const head = document.createElement("div");
  head.className = "portal-record-head";
  const titleWrap = document.createElement("div");
  titleWrap.append(createTextElement("span", "portal-record-type", "MAINTENANCE"), createTextElement("h3", "", maintenance.title || "メンテナンス"));
  head.append(titleWrap, createTextElement("span", `portal-state portal-state-${safeText(maintenance.state, "unknown")}`, MAINTENANCE_STATE_COPY[maintenance.state] || "状態確認中"));
  card.append(head, createTextElement("p", "portal-record-summary", maintenance.summary || "公開された詳細情報はありません。"));

  const meta = document.createElement("div");
  meta.className = "portal-record-meta";
  appendMetaRow(meta, "Public ID", maintenance.public_id);
  appendMetaRow(meta, "対象", serviceListText(maintenance, portalState.snapshot));
  appendMetaRow(meta, "開始", formatDateTime(maintenance.starts_at));
  appendMetaRow(meta, "終了", formatDateTime(maintenance.ends_at));
  card.append(meta, createShareActions(maintenance, { history }));
  return card;
}

function createAnnouncementCard(announcement, { compact = false, history = false } = {}) {
  const card = document.createElement("article");
  card.className = `portal-record-card portal-announcement-card${compact ? " is-compact" : ""}`;
  card.id = `public-${safeText(announcement.public_id)}`;
  card.tabIndex = -1;
  card.dataset.publicId = safeText(announcement.public_id);
  card.dataset.recordType = "announcement";

  const head = document.createElement("div");
  head.className = "portal-record-head";
  const titleWrap = document.createElement("div");
  titleWrap.append(createTextElement("span", "portal-record-type", "ANNOUNCEMENT"), createTextElement("h3", "", announcement.title || "お知らせ"));
  head.append(titleWrap, createTextElement("span", `portal-state portal-state-${safeText(announcement.kind, "info")}`, ANNOUNCEMENT_KIND_COPY[announcement.kind] || "お知らせ"));
  card.append(head, createTextElement("p", "portal-record-summary portal-preserve-lines", announcement.body || ""));

  const meta = document.createElement("div");
  meta.className = "portal-record-meta";
  appendMetaRow(meta, "Public ID", announcement.public_id);
  appendMetaRow(meta, "対象", serviceListText(announcement, portalState.snapshot));
  appendMetaRow(meta, "公開", formatDateTime(announcement.published_at));
  if (announcement.expires_at) appendMetaRow(meta, "掲載期限", formatDateTime(announcement.expires_at));
  card.append(meta, createShareActions(announcement, { history }));
  return card;
}

function createPortalSection({ id, label, title, description }) {
  const section = document.createElement("section");
  section.className = "section-block portal-section";
  section.id = id;
  section.hidden = true;
  const heading = document.createElement("div");
  heading.className = "section-heading";
  const titleWrap = document.createElement("div");
  titleWrap.append(createTextElement("p", "section-label", label), createTextElement("h2", "", title));
  heading.append(titleWrap, createTextElement("p", "", description));
  const content = document.createElement("div");
  content.className = "portal-record-list";
  content.id = `${id}List`;
  heading.querySelector("h2").id = `${id}Title`;
  section.setAttribute("aria-labelledby", `${id}Title`);
  section.append(heading, content);
  return section;
}

function ensureHomeInformationArchitecture() {
  const summary = document.querySelector(".summary-strip");
  const services = $("services");
  const incidents = $("incidents");
  if (!summary || !services || !incidents) return;

  let activeSection = $("activePublicStatus");
  if (!activeSection) {
    activeSection = createPortalSection({
      id: "activePublicStatus",
      label: "ACTIVE",
      title: "現在のお知らせ",
      description: "進行中の障害・メンテナンスを掲載します。",
    });
    summary.after(activeSection);
  }

  let upcomingSection = $("upcomingMaintenance");
  if (!upcomingSection) {
    upcomingSection = createPortalSection({
      id: "upcomingMaintenance",
      label: "UPCOMING MAINTENANCE",
      title: "予定されているメンテナンス",
      description: "今後予定されている公開メンテナンスを掲載します。",
    });
    activeSection.after(upcomingSection);
  }

  const headingLabel = incidents.querySelector(".section-label");
  const headingTitle = $("incidentsTitle");
  const headingDescription = incidents.querySelector(".section-heading > p");
  if (headingLabel) headingLabel.textContent = "RECENT UPDATES";
  if (headingTitle) headingTitle.textContent = "最近の障害・お知らせ";
  if (headingDescription) headingDescription.textContent = "復旧済みの障害と公開中のお知らせを新しいものから掲載します。";

  const legacyRecentList = $("incidentList");
  if (legacyRecentList) {
    legacyRecentList.hidden = true;
    let portalRecentList = $("portalRecentList");
    if (!portalRecentList) {
      portalRecentList = document.createElement("div");
      portalRecentList.id = "portalRecentList";
      portalRecentList.className = "incident-list";
      portalRecentList.setAttribute("aria-live", "polite");
      legacyRecentList.after(portalRecentList);
    }
  }

  const historyRoute = document.querySelector(".history-route");
  if (historyRoute && incidents.nextElementSibling !== historyRoute) incidents.after(historyRoute);

  ensureContentFreshnessNote(summary);
}

function ensureContentFreshnessNote(summary) {
  let note = $("contentFreshnessNote");
  if (!note) {
    note = createTextElement("p", "portal-content-note", "");
    note.id = "contentFreshnessNote";
    note.hidden = true;
    summary.after(note);
  }
}

function renderHomeSections(snapshot) {
  const { incidents, maintenance, announcements, contentMeta } = normalizePublicCollections(snapshot);
  const activeIncidents = incidents.filter((item) => safeText(item?.status).toLowerCase() !== "resolved");
  const inProgressMaintenance = maintenance.filter((item) => safeText(item?.state).toLowerCase() === "in_progress");
  const upcomingMaintenance = maintenance
    .filter((item) => safeText(item?.state).toLowerCase() === "scheduled")
    .sort((a, b) => new Date(a.starts_at || 0) - new Date(b.starts_at || 0));
  const resolvedIncidents = incidents.filter((item) => safeText(item?.status).toLowerCase() === "resolved");
  const activeAnnouncements = announcements.filter((item) => item?.active !== false);

  const activeSection = $("activePublicStatus");
  const activeList = $("activePublicStatusList");
  activeList?.replaceChildren();
  for (const incident of activeIncidents) activeList?.append(createIncidentCard(incident));
  for (const item of inProgressMaintenance) activeList?.append(createMaintenanceCard(item));
  if (activeSection) activeSection.hidden = activeIncidents.length + inProgressMaintenance.length === 0;

  const upcomingSection = $("upcomingMaintenance");
  const upcomingList = $("upcomingMaintenanceList");
  upcomingList?.replaceChildren();
  for (const item of upcomingMaintenance.slice(0, 6)) upcomingList?.append(createMaintenanceCard(item, { compact: true }));
  if (upcomingSection) upcomingSection.hidden = upcomingMaintenance.length === 0;

  const recentList = $("portalRecentList");
  if (recentList) {
    recentList.replaceChildren();
    const recentRecords = [
      ...resolvedIncidents.map((record) => ({ record, at: new Date(record.updated_at || record.resolved_at || record.started_at || 0).getTime() })),
      ...activeAnnouncements.map((record) => ({ record, at: new Date(record.published_at || 0).getTime() })),
    ]
      .filter(({ record }) => {
        const publicId = safeText(record?.public_id);
        return INCIDENT_ID_PATTERN.test(publicId) || ANNOUNCEMENT_ID_PATTERN.test(publicId);
      })
      .sort((a, b) => b.at - a.at)
      .slice(0, 8);

    if (!recentRecords.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      const copy = document.createElement("div");
      copy.append(createTextElement("strong", "", "最近の公開情報はありません"), createTextElement("p", "", "復旧済みの障害やお知らせが公開された場合、ここに表示します。"));
      empty.append(copy);
      recentList.append(empty);
    } else {
      for (const { record } of recentRecords) {
        if (INCIDENT_ID_PATTERN.test(safeText(record.public_id))) recentList.append(createIncidentCard(record));
        else recentList.append(createAnnouncementCard(record));
      }
    }
  }

  const note = $("contentFreshnessNote");
  if (note) {
    const source = safeText(contentMeta?.source, "none");
    const stale = Boolean(contentMeta?.stale);
    if (source === "cache" || stale) {
      note.hidden = false;
      note.textContent = stale
        ? "公開情報はキャッシュ済みデータを表示しています。ライブ稼働状況は独立して更新されます。"
        : "公開情報の一部はキャッシュから表示しています。ライブ稼働状況は独立して更新されます。";
    } else {
      note.hidden = true;
      note.textContent = "";
    }
  }
}

function incidentById(snapshot, publicId) {
  return asArray(snapshot?.incidents).find((incident) => safeText(incident?.public_id) === publicId) || null;
}

function ensureTimelinePopover() {
  let popover = $("timelinePopover");
  if (popover) return popover;

  popover = document.createElement("div");
  popover.id = "timelinePopover";
  popover.className = "timeline-popover";
  popover.setAttribute("role", "dialog");
  popover.setAttribute("aria-modal", "false");
  popover.setAttribute("aria-label", "時間帯の稼働詳細");
  popover.hidden = true;
  popover.tabIndex = -1;
  popover.addEventListener("mouseenter", () => window.clearTimeout(portalState.closeTimer));
  popover.addEventListener("mouseleave", schedulePopoverClose);
  document.body.append(popover);
  return popover;
}

function schedulePopoverClose() {
  window.clearTimeout(portalState.closeTimer);
  portalState.closeTimer = window.setTimeout(() => {
    const popover = $("timelinePopover");
    if (!popover?.contains(document.activeElement)) closeTimelinePopover();
  }, 140);
}

function closeTimelinePopover({ restoreFocus = false } = {}) {
  const popover = $("timelinePopover");
  if (!popover || popover.hidden) return;
  const anchor = portalState.activePopoverAnchor;
  popover.hidden = true;
  popover.replaceChildren();
  if (anchor) anchor.setAttribute("aria-expanded", "false");
  portalState.activePopoverAnchor = null;
  if (restoreFocus && anchor?.isConnected) {
    portalState.restoringPopoverFocus = true;
    try {
      anchor.focus({ preventScroll: true });
    } finally {
      portalState.restoringPopoverFocus = false;
    }
  }
}

function positionPopover(anchor, popover) {
  const rect = anchor.getBoundingClientRect();
  const margin = 12;
  const width = Math.min(360, window.innerWidth - margin * 2);
  popover.style.width = `${width}px`;
  popover.style.left = `${Math.max(margin, Math.min(window.innerWidth - width - margin, rect.left + rect.width / 2 - width / 2))}px`;
  popover.style.top = `${Math.max(margin, Math.min(window.innerHeight - popover.offsetHeight - margin, rect.bottom + 10))}px`;
}

function openTimelinePopover(anchor, bucket, service) {
  window.clearTimeout(portalState.closeTimer);
  const popover = ensureTimelinePopover();
  if (portalState.activePopoverAnchor && portalState.activePopoverAnchor !== anchor) {
    portalState.activePopoverAnchor.setAttribute("aria-expanded", "false");
  }
  portalState.activePopoverAnchor = anchor;
  anchor.setAttribute("aria-expanded", "true");
  popover.replaceChildren();

  const top = document.createElement("div");
  top.className = "timeline-popover-head";
  const copy = document.createElement("div");
  copy.append(createTextElement("span", "portal-record-type", safeText(service?.name, "SERVICE")), createTextElement("strong", "", STATUS_COPY[bucket.status] || "状態確認中"));
  const closeButton = createTextElement("button", "timeline-popover-close", "閉じる");
  closeButton.type = "button";
  closeButton.addEventListener("click", () => closeTimelinePopover({ restoreFocus: true }));
  top.append(copy, closeButton);
  popover.append(top, createTextElement("p", "timeline-popover-time", formatBucketRange(bucket)));

  const incidentIds = asArray(bucket.related_incident_ids).filter((id) => INCIDENT_ID_PATTERN.test(id));
  const related = incidentIds.map((id) => incidentById(portalState.snapshot, id)).filter(Boolean);
  if (!related.length) {
    popover.append(createTextElement("p", "timeline-popover-empty", "公開された原因情報はありません"));
  } else {
    for (const incident of related) {
      const detail = document.createElement("div");
      detail.className = "timeline-popover-incident";
      detail.append(createTextElement("strong", "", incident.title || "障害情報"), createTextElement("p", "", incident.summary || bucket.summary || "公開された詳細情報はありません。"));
      const meta = document.createElement("div");
      meta.className = "portal-record-meta";
      appendMetaRow(meta, "状態", INCIDENT_STATUS_COPY[incident.status] || safeText(incident.status));
      appendMetaRow(meta, "発生", formatDateTime(incident.started_at));
      if (incident.resolved_at) appendMetaRow(meta, "復旧", formatDateTime(incident.resolved_at));
      const link = createTextElement("a", "portal-detail-link", "Incident詳細を見る");
      link.href = `/?incident=${encodeURIComponent(incident.public_id)}`;
      detail.append(meta, link);
      popover.append(detail);
    }
  }

  popover.hidden = false;
  requestAnimationFrame(() => positionPopover(anchor, popover));
}

function timelineAriaLabel(service, bucket) {
  const related = asArray(bucket.related_incident_ids).filter((id) => INCIDENT_ID_PATTERN.test(id));
  const reason = related.length ? safeText(bucket.summary, "公開Incidentあり") : "公開された原因情報はありません";
  return `${safeText(service?.name, "サービス")} ${formatBucketRange(bucket)} ${STATUS_COPY[bucket.status] || "状態確認中"}。${reason}`;
}

function enhanceTimelineBars(snapshot) {
  const rows = [...document.querySelectorAll(".service-row")];
  if (!rows.length || !snapshot) return;
  const services = asArray(snapshot.services);

  for (const row of rows) {
    const name = row.querySelector(".service-identity strong")?.textContent?.trim();
    const service = services.find((item) => safeText(item?.name) === name);
    if (!service) continue;
    const details = normalizeTimelineDetails(service);
    const bars = [...row.querySelectorAll(".uptime-bars .uptime-bar")];
    bars.forEach((bar, index) => {
      if (bar.dataset.portalReady === "true") return;
      const bucket = details[index] || details.at(-1);
      const button = document.createElement("button");
      button.type = "button";
      button.className = bar.className;
      button.dataset.portalReady = "true";
      button.setAttribute("aria-haspopup", "dialog");
      button.setAttribute("aria-expanded", "false");
      button.setAttribute("aria-label", timelineAriaLabel(service, bucket));
      button.addEventListener("mouseenter", () => openTimelinePopover(button, bucket, service));
      button.addEventListener("mouseleave", schedulePopoverClose);
      button.addEventListener("focus", () => {
        if (!portalState.restoringPopoverFocus) openTimelinePopover(button, bucket, service);
      });
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        openTimelinePopover(button, bucket, service);
      });
      bar.replaceWith(button);
    });
  }
}

function findDeepLinkedRecord(snapshot, deepLink) {
  if (!deepLink?.valid) return null;
  if (deepLink.kind === "incident") return asArray(snapshot?.incidents).find((record) => safeText(record?.public_id) === deepLink.id) || null;
  if (deepLink.kind === "maintenance") return asArray(snapshot?.maintenance).find((record) => safeText(record?.public_id) === deepLink.id) || null;
  if (deepLink.kind === "announcement") return asArray(snapshot?.announcements).find((record) => safeText(record?.public_id) === deepLink.id) || null;
  return null;
}

function createPublicRecordCard(record) {
  const publicId = safeText(record?.public_id);
  if (INCIDENT_ID_PATTERN.test(publicId)) return createIncidentCard(record);
  if (MAINTENANCE_ID_PATTERN.test(publicId)) return createMaintenanceCard(record);
  if (ANNOUNCEMENT_ID_PATTERN.test(publicId)) return createAnnouncementCard(record);
  return null;
}

function ensureDeepLinkedRecord(record) {
  const publicId = safeText(record?.public_id);
  const existing = document.getElementById(`public-${publicId}`);
  if (existing) return existing;

  const card = createPublicRecordCard(record);
  const upcomingSection = $("upcomingMaintenance");
  if (!card || !upcomingSection) return null;

  let section = $("deepLinkedPublicRecord");
  if (!section) {
    section = createPortalSection({
      id: "deepLinkedPublicRecord",
      label: "SHARED RECORD",
      title: "共有された公開情報",
      description: "共有URLで指定された公開情報を表示しています。",
    });
    upcomingSection.after(section);
  }

  const list = $("deepLinkedPublicRecordList");
  list?.replaceChildren(card);
  section.hidden = false;
  return card;
}

function handleDeepLink(snapshot) {
  if (portalState.deepLinkHandled) return;
  portalState.deepLinkHandled = true;
  const deepLink = parsePortalDeepLink(window.location.search);
  if (!deepLink) return;
  if (!deepLink.valid) {
    showPortalToast("共有URLのID形式が正しくないため、通常表示に戻しました");
    return;
  }
  const record = findDeepLinkedRecord(snapshot, deepLink);
  if (!record) {
    showPortalToast("指定された公開情報は見つかりませんでした");
    return;
  }
  const target = ensureDeepLinkedRecord(record);
  if (!target) return;
  target.classList.add("is-deep-linked");
  target.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
  target.focus({ preventScroll: true });
}

async function loadPortalSnapshot() {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`/api/status.json?portal=${Date.now()}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!data || !Array.isArray(data.services)) throw new Error("invalid response");
    return data;
  } catch {
    return null;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function refreshPortal({ announce = false } = {}) {
  if (portalState.refreshing) return;
  portalState.refreshing = true;
  try {
    const snapshot = await loadPortalSnapshot();
    if (!snapshot) {
      if (announce) showPortalToast("公開情報を取得できませんでした");
      return;
    }
    portalState.snapshot = snapshot;
    ensureHomeInformationArchitecture();
    renderHomeSections(snapshot);
    enhanceTimelineBars(snapshot);
    handleDeepLink(snapshot);
    if (announce) showPortalToast("公開情報を更新しました");
  } finally {
    portalState.refreshing = false;
  }
}

function bindPortalGlobalInteractions() {
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeTimelinePopover({ restoreFocus: true });
  });
  document.addEventListener("focusin", (event) => {
    const popover = $("timelinePopover");
    if (!popover || popover.hidden) return;
    const target = event.target;
    if (popover.contains(target)) return;
    if (target === portalState.activePopoverAnchor) return;
    if (target instanceof Element && target.closest("button.uptime-bar[data-portal-ready='true']")) return;
    closeTimelinePopover();
  });
  document.addEventListener("pointerdown", (event) => {
    const popover = $("timelinePopover");
    if (!popover || popover.hidden) return;
    if (popover.contains(event.target) || portalState.activePopoverAnchor?.contains(event.target)) return;
    closeTimelinePopover();
  });
  window.addEventListener("resize", () => {
    const popover = $("timelinePopover");
    if (popover && !popover.hidden && portalState.activePopoverAnchor) positionPopover(portalState.activePopoverAnchor, popover);
  });
  window.addEventListener("scroll", () => {
    const popover = $("timelinePopover");
    if (popover && !popover.hidden && portalState.activePopoverAnchor) positionPopover(portalState.activePopoverAnchor, popover);
  }, { passive: true });
  $("refreshButton")?.addEventListener("click", () => window.setTimeout(() => refreshPortal({ announce: true }), 250));
}

ensurePortalStyles();
ensureHomeInformationArchitecture();
bindPortalGlobalInteractions();

const serviceGroups = $("serviceGroups");
if (serviceGroups) {
  const observer = new MutationObserver(() => {
    updateStatusPresentation();
    if (portalState.snapshot) enhanceTimelineBars(portalState.snapshot);
  });
  observer.observe(serviceGroups, { childList: true, subtree: true });
  updateStatusPresentation();
}

refreshPortal();