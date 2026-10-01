const ENDPOINTS = {
  publicStatus: "/api/status.json",
  current: "/api/current.json",
  history: "/api/history.json",
};

const REFRESH_INTERVAL_MS = 60_000;
const STALE_AFTER_SECONDS = 300;

const state = {
  snapshot: null,
  refreshTimer: null,
  countdownTimer: null,
  nextRefreshAt: 0,
  refreshing: false,
};

const $ = (id) => document.getElementById(id);

const STATUS_PRIORITY = {
  operational: 0,
  maintenance: 1,
  degraded: 2,
  outage: 3,
  unknown: 4,
};

const STATUS_COPY = {
  operational: "正常稼働",
  maintenance: "メンテナンス中",
  degraded: "一部影響あり",
  outage: "障害発生中",
  unknown: "確認中",
};

const OVERALL_COPY = {
  operational: {
    eyebrow: "ALL SYSTEMS OPERATIONAL",
    title: "すべてのシステムは正常です",
    message: "現在、ivRooomのサービスに利用者影響のある障害は確認されていません。",
  },
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
  unknown: {
    eyebrow: "STATUS UNAVAILABLE",
    title: "最新の状態を確認できません",
    message: "ステータスデータの取得に失敗しました。時間をおいて再度ご確認ください。",
  },
};

function normalizeStatus(value) {
  const status = String(value || "unknown").toLowerCase();
  if (["online", "ok", "healthy", "up"].includes(status)) return "operational";
  if (["warning", "partial", "degraded_performance"].includes(status)) return "degraded";
  if (["maintenance", "scheduled_maintenance"].includes(status)) return "maintenance";
  if (["offline", "down", "major_outage", "critical"].includes(status)) return "outage";
  return Object.hasOwn(STATUS_PRIORITY, status) ? status : "unknown";
}

function worstStatus(statuses) {
  return statuses.reduce((worst, status) => {
    const normalized = normalizeStatus(status);
    return STATUS_PRIORITY[normalized] > STATUS_PRIORITY[worst] ? normalized : worst;
  }, "operational");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDateTime(value, includeSeconds = false) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("ja-JP", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: includeSeconds ? "2-digit" : undefined,
    hour12: false,
  }).format(date);
}

function formatTime(value) {
  if (!value) return "--:--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--";
  return new Intl.DateTimeFormat("ja-JP", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function secondsSince(value) {
  if (!value) return Infinity;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return Infinity;
  return Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
}

function freshnessLabel(seconds) {
  if (!Number.isFinite(seconds)) return "更新時刻不明";
  if (seconds < 60) return `${seconds}秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}分前`;
  return `${Math.floor(seconds / 3600)}時間前`;
}

async function fetchJson(url) {
  const response = await fetch(`${url}?t=${Date.now()}`, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  }).catch(() => null);

  if (!response?.ok) return null;

  try {
    return await response.json();
  } catch {
    return null;
  }
}

function bucketHistory(history, bucketCount = 24) {
  if (!Array.isArray(history) || history.length === 0) {
    return Array.from({ length: bucketCount }, () => "unknown");
  }

  const now = Date.now();
  const bucketMs = 60 * 60 * 1000;

  return Array.from({ length: bucketCount }, (_, index) => {
    const end = now - (bucketCount - 1 - index) * bucketMs;
    const start = end - bucketMs;
    const entries = history.filter((item) => {
      const time = new Date(item.collected_at).getTime();
      return Number.isFinite(time) && time > start && time <= end;
    });

    if (entries.length === 0) return "unknown";
    return worstStatus(entries.map((entry) => entry.status));
  });
}

function constantTimeline(status, bucketCount = 24) {
  return Array.from({ length: bucketCount }, () => normalizeStatus(status));
}

function createFallbackSnapshot(current, history) {
  if (!current) return null;

  const collectedAt = current.collected_at || new Date().toISOString();
  const stale = secondsSince(collectedAt) > STALE_AFTER_SECONDS;
  const minecraftStatus = stale ? "degraded" : normalizeStatus(current.status);
  const backupTimer = current.timers?.["mc-backup-s3.timer"] || {};
  const backupAge = secondsSince(current.backup?.latest_local?.mtime);
  const backupActive = String(backupTimer.active || "").toLowerCase() === "active";
  const backupStatus = current.backup?.latest_local && backupAge < 86_400 && backupActive
    ? "operational"
    : current.backup?.latest_local
      ? "degraded"
      : "unknown";
  const statusDataStatus = stale ? "degraded" : "operational";

  const services = [
    {
      id: "minecraft-network",
      group: "ゲームサービス",
      name: "Minecraft Network",
      description: current.server?.name
        ? `${current.server.name} — ${current.server?.mode || "Minecraft Server"}`
        : "Minecraftサーバー",
      status: minecraftStatus,
      timeline: bucketHistory(history),
      meta: {
        type: "minecraft",
        connection: current.server?.connection || "mc.ivrm.jp",
        playersOnline: Number(current.players?.online || 0),
        playersMax: Number(current.players?.max || current.settings?.["max-players"] || 0),
        mode: current.server?.mode || "Minecraft Server",
      },
    },
    {
      id: "status-data",
      group: "プラットフォーム",
      name: "Status Data",
      description: "サービス状態を公開するリアルタイムデータフィード",
      status: statusDataStatus,
      timeline: constantTimeline(statusDataStatus),
    },
    {
      id: "data-protection",
      group: "プラットフォーム",
      name: "Data Protection",
      description: "ゲームデータのバックアップと保護",
      status: backupStatus,
      timeline: constantTimeline(backupStatus),
    },
  ];

  return {
    generated_at: collectedAt,
    overall_status: worstStatus(services.map((service) => service.status)),
    services,
    incidents: [],
    source: "legacy-adapter",
  };
}

function normalizePublicSnapshot(data) {
  const services = Array.isArray(data?.services)
    ? data.services.map((service, index) => ({
        id: service.id || `service-${index + 1}`,
        group: service.group || "サービス",
        name: service.name || "名称未設定",
        description: service.description || "",
        status: normalizeStatus(service.status),
        timeline: Array.isArray(service.timeline)
          ? service.timeline.slice(-24).map(normalizeStatus)
          : constantTimeline(service.status),
        meta: service.meta || {},
      }))
    : [];

  services.forEach((service) => {
    while (service.timeline.length < 24) service.timeline.unshift("unknown");
  });

  return {
    generated_at: data?.generated_at || data?.collected_at || new Date().toISOString(),
    overall_status: normalizeStatus(
      data?.overall_status || (services.length ? worstStatus(services.map((service) => service.status)) : "unknown"),
    ),
    message: data?.message || "",
    services,
    incidents: Array.isArray(data?.incidents) ? data.incidents : [],
    source: "public-status-api",
  };
}

function statusIcon(id) {
  if (id.includes("minecraft")) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 7 8-4 8 4v10l-8 4-8-4z"/><path d="m4 7 8 4 8-4M12 11v10"/></svg>';
  }
  if (id.includes("data") || id.includes("api")) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6c0-1.1 3.6-2 8-2s8 .9 8 2-3.6 2-8 2-8-.9-8-2Z"/><path d="M4 6v6c0 1.1 3.6 2 8 2s8-.9 8-2V6M4 12v6c0 1.1 3.6 2 8 2s8-.9 8-2v-6"/></svg>';
  }
  if (id.includes("backup") || id.includes("protection")) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 4 6v5c0 5 3.4 8.7 8 10 4.6-1.3 8-5 8-10V6z"/><path d="m8.5 12 2.2 2.2 4.8-5"/></svg>';
  }
  return '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/></svg>';
}

function renderServiceGroups(services) {
  const container = $("serviceGroups");
  if (!services.length) {
    container.innerHTML = `<div class="empty-state"><span class="empty-state-icon" aria-hidden="true">${statusIcon("status")}</span><div><strong>サービス情報を取得できません</strong><p>しばらくしてから再度お試しください。</p></div></div>`;
    return;
  }

  const grouped = services.reduce((groups, service) => {
    const group = service.group || "サービス";
    groups[group] ||= [];
    groups[group].push(service);
    return groups;
  }, {});

  container.innerHTML = Object.entries(grouped)
    .map(([groupName, groupServices]) => `<article class="service-group"><header class="service-group-header"><h3>${escapeHtml(groupName)}</h3><span>${groupServices.length} service${groupServices.length === 1 ? "" : "s"}</span></header>${groupServices.map(renderServiceRow).join("")}</article>`)
    .join("") + UPTIME_LEGEND;
}

const UPTIME_LEGEND = `<ul class="uptime-legend" aria-label="タイムラインの凡例">${[
  ["operational", "正常"],
  ["degraded", "一部影響・メンテナンス"],
  ["outage", "障害"],
  ["unknown", "データなし"],
].map(([status, label]) => `<li><i class="${status}" aria-hidden="true"></i>${label}</li>`).join("")}</ul>`;

// Hourly buckets: one affected minute marks the whole hour, so report hours rather than a percentage.
function healthyHoursLabel(timeline) {
  const known = timeline.map(normalizeStatus).filter((status) => status !== "unknown");
  if (!known.length) return "データなし";
  const up = known.filter((status) => status === "operational").length;
  return `正常 ${up} / ${known.length}時間`;
}

function renderServiceRow(service) {
  const bars = (service.timeline.length ? service.timeline : constantTimeline(service.status))
    .slice(-24)
    .map((status, index) => `<span class="uptime-bar ${normalizeStatus(status)}" title="${index + 1}時間帯: ${STATUS_COPY[normalizeStatus(status)]}"></span>`)
    .join("");


  return `<div class="service-row"><div class="service-identity"><span class="service-icon">${statusIcon(service.id)}</span><div><strong>${escapeHtml(service.name)}</strong><p>${escapeHtml(service.description)}</p></div></div><div class="uptime-block" aria-label="${escapeHtml(service.name)}の直近24時間"><div class="uptime-bars">${bars}</div><div class="uptime-caption"><span>24時間前</span><span class="uptime-percent">${healthyHoursLabel(service.timeline)}</span><span>現在</span></div></div><span class="service-state ${service.status}">${STATUS_COPY[service.status]}</span></div>`;
}

function renderMinecraftFeature(services) {
  const service = services.find((item) => item.meta?.type === "minecraft" || item.id.includes("minecraft"));
  const feature = $("minecraftFeature");

  if (!service?.meta) {
    feature.hidden = true;
    return;
  }

  feature.hidden = false;
  $("minecraftMode").textContent = service.meta.mode || service.description || "Minecraft Server";
  $("playersOnline").textContent = Number(service.meta.playersOnline || 0);
  $("playersMax").textContent = Number(service.meta.playersMax || 0) || "--";
  $("serverAddress").textContent = service.meta.connection || "mc.ivrm.jp";
}

function renderIncidents(incidents) {
  const container = $("incidentList");
  const active = incidents.filter((incident) => !["resolved", "completed"].includes(String(incident.status || "").toLowerCase()));
  const sorted = [...incidents].sort((a, b) => new Date(b.updated_at || b.started_at || 0) - new Date(a.updated_at || a.started_at || 0));

  if (!sorted.length) {
    container.innerHTML = '<div class="empty-state"><span class="empty-state-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m5 12 4 4L19 6"/></svg></span><div><strong>現在、掲載中の障害情報はありません</strong><p>利用者影響のある障害やメンテナンスが発生した場合、このページでお知らせします。</p></div></div>';
    $("activeIncidentCount").textContent = "0";
    return;
  }

  $("activeIncidentCount").textContent = String(active.length);
  container.innerHTML = sorted.slice(0, 8).map((incident) => {
    const impact = String(incident.impact || "minor").toLowerCase();
    return `<article class="incident-card" data-impact="${escapeHtml(impact)}"><div class="incident-card-main"><span class="incident-icon" aria-hidden="true">${statusIcon("incident")}</span><div><strong>${escapeHtml(incident.title || "障害情報")}</strong><p>${escapeHtml(incident.message || incident.summary || "詳細を確認しています。")}</p></div></div><time datetime="${escapeHtml(incident.updated_at || incident.started_at || "")}">${formatDateTime(incident.updated_at || incident.started_at)}</time></article>`;
  }).join("");
}

function render(snapshot) {
  state.snapshot = snapshot;

  const overall = snapshot?.overall_status || "unknown";
  const copy = OVERALL_COPY[overall] || OVERALL_COPY.unknown;
  const collectedSeconds = secondsSince(snapshot?.generated_at);

  document.body.dataset.overallStatus = overall;
  $("overallEyebrow").textContent = copy.eyebrow;
  $("overallTitle").textContent = copy.title;
  $("overallMessage").textContent = snapshot?.message?.trim() || copy.message;
  $("lastUpdated").textContent = formatTime(snapshot?.generated_at);
  $("freshnessText").textContent = freshnessLabel(collectedSeconds);
  $("footerTimestamp").textContent = `Updated ${formatDateTime(snapshot?.generated_at, true)}`;

  const services = snapshot?.services || [];
  const operationalCount = services.filter((service) => service.status === "operational").length;
  const activeIncidents = (snapshot?.incidents || []).filter((incident) => !["resolved", "completed"].includes(String(incident.status || "").toLowerCase()));

  $("serviceCount").textContent = String(services.length);
  $("operationalCount").textContent = `${operationalCount} / ${services.length}`;
  $("activeIncidentCount").textContent = String(activeIncidents.length);

  renderServiceGroups(services);
  renderMinecraftFeature(services);
  renderIncidents(snapshot?.incidents || []);
}

async function loadSnapshot() {
  const publicStatus = await fetchJson(ENDPOINTS.publicStatus);
  if (publicStatus?.services) return normalizePublicSnapshot(publicStatus);

  const [current, history] = await Promise.all([
    fetchJson(ENDPOINTS.current),
    fetchJson(ENDPOINTS.history),
  ]);
  const historyArray = Array.isArray(history) ? history : Array.isArray(history?.history) ? history.history : [];
  return createFallbackSnapshot(current, historyArray);
}

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("visible");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 2400);
}

async function refresh({ announce = false } = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  $("refreshButton").classList.add("loading");
  $("refreshButton").disabled = true;

  try {
    const snapshot = await loadSnapshot();
    if (snapshot) {
      render(snapshot);
      if (announce) showToast("最新のステータスに更新しました");
    } else {
      render({ generated_at: new Date().toISOString(), overall_status: "unknown", services: [], incidents: [] });
      if (announce) showToast("ステータスデータを取得できませんでした");
    }
  } finally {
    state.refreshing = false;
    $("refreshButton").classList.remove("loading");
    $("refreshButton").disabled = false;
    scheduleRefresh();
  }
}

function scheduleRefresh() {
  window.clearTimeout(state.refreshTimer);
  window.clearInterval(state.countdownTimer);
  state.nextRefreshAt = Date.now() + REFRESH_INTERVAL_MS;

  const updateCountdown = () => {
    const remaining = Math.max(0, Math.ceil((state.nextRefreshAt - Date.now()) / 1000));
    $("nextRefresh").textContent = remaining > 0 ? `${remaining}秒後` : "更新中";
  };

  updateCountdown();
  state.countdownTimer = window.setInterval(updateCountdown, 1000);
  state.refreshTimer = window.setTimeout(() => refresh(), REFRESH_INTERVAL_MS);
}

$("refreshButton")?.addEventListener("click", () => refresh({ announce: true }));

$("copyAddressButton")?.addEventListener("click", async () => {
  const address = $("serverAddress")?.textContent?.trim();
  if (!address) return;

  try {
    await navigator.clipboard.writeText(address);
    showToast(`${address} をコピーしました`);
  } catch {
    showToast(`接続先: ${address}`);
  }
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.snapshot && secondsSince(state.snapshot.generated_at) > 120) {
    refresh();
  }
});

refresh();
