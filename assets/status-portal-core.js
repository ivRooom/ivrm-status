export const INCIDENT_ID_PATTERN = /^INC-[A-F0-9]{12}$/;
export const MAINTENANCE_ID_PATTERN = /^MNT-[A-F0-9]{12}$/;
export const ANNOUNCEMENT_ID_PATTERN = /^ANN-[A-F0-9]{12}$/;

const SAFE_SERVICE_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;

export function asArray(value) {
  return Array.isArray(value) ? value : [];
}

export function safeText(value, fallback = "") {
  if (value === null || value === undefined) return fallback;
  const text = String(value).trim();
  return text || fallback;
}

export function classifyPublicId(value) {
  const id = safeText(value);
  if (INCIDENT_ID_PATTERN.test(id)) return "incident";
  if (MAINTENANCE_ID_PATTERN.test(id)) return "maintenance";
  if (ANNOUNCEMENT_ID_PATTERN.test(id)) return "announcement";
  return null;
}

export function isSafeServiceId(value) {
  return SAFE_SERVICE_ID_PATTERN.test(safeText(value));
}

export function parsePortalDeepLink(search = "") {
  const params = new URLSearchParams(search);
  const incident = params.get("incident");
  const notice = params.get("notice");

  if (incident !== null) {
    return INCIDENT_ID_PATTERN.test(incident)
      ? { kind: "incident", id: incident, valid: true }
      : { kind: "incident", id: incident, valid: false };
  }

  if (notice !== null) {
    const kind = classifyPublicId(notice);
    if (kind === "maintenance" || kind === "announcement") {
      return { kind, id: notice, valid: true };
    }
    return { kind: "notice", id: notice, valid: false };
  }

  return null;
}

export function normalizePublicCollections(data) {
  return {
    incidents: asArray(data?.incidents),
    maintenance: asArray(data?.maintenance),
    announcements: asArray(data?.announcements),
    contentMeta: data?.content_meta && typeof data.content_meta === "object" ? data.content_meta : null,
  };
}

export function normalizeTimelineDetails(service) {
  const details = asArray(service?.timeline_details).slice(-24).map((bucket) => ({
    start_at: safeText(bucket?.start_at),
    end_at: safeText(bucket?.end_at),
    status: safeText(bucket?.status, "unknown").toLowerCase(),
    related_incident_ids: asArray(bucket?.related_incident_ids)
      .map((id) => safeText(id))
      .filter((id) => INCIDENT_ID_PATTERN.test(id)),
    summary: safeText(bucket?.summary),
  }));

  while (details.length < 24) {
    details.unshift({
      start_at: "",
      end_at: "",
      status: "unknown",
      related_incident_ids: [],
      summary: "",
    });
  }
  return details;
}

export function publicRecordType(record) {
  return classifyPublicId(record?.public_id);
}

export function publicRecordStatus(record) {
  const type = publicRecordType(record);
  if (type === "incident") return safeText(record?.status, "unknown").toLowerCase();
  if (type === "maintenance") return safeText(record?.state, "unknown").toLowerCase();
  if (type === "announcement") return safeText(record?.kind, "info").toLowerCase();
  return "unknown";
}

export function publicRecordTimestamp(record) {
  const type = publicRecordType(record);
  const value = type === "incident"
    ? record?.started_at
    : type === "maintenance"
      ? record?.starts_at
      : record?.published_at;
  const time = new Date(value || 0).getTime();
  return Number.isFinite(time) ? time : 0;
}

export function publicRecordServiceIds(record) {
  return asArray(record?.affected_service_ids)
    .map((id) => safeText(id))
    .filter(isSafeServiceId);
}

export function combineArchiveRecords(data) {
  const { incidents, maintenance, announcements } = normalizePublicCollections(data);
  return [...incidents, ...maintenance, ...announcements]
    .filter((record) => publicRecordType(record))
    .sort((a, b) => publicRecordTimestamp(b) - publicRecordTimestamp(a));
}

export function filterArchiveRecords(records, filters = {}) {
  const type = safeText(filters.type, "all");
  const status = safeText(filters.status, "all");
  const service = safeText(filters.service, "all");
  const fromDate = safeText(filters.fromDate);
  const toDate = safeText(filters.toDate);
  const fromTime = fromDate ? new Date(`${fromDate}T00:00:00+09:00`).getTime() : null;
  const toTime = toDate ? new Date(`${toDate}T23:59:59.999+09:00`).getTime() : null;

  return asArray(records).filter((record) => {
    const recordType = publicRecordType(record);
    if (!recordType) return false;
    if (type !== "all" && recordType !== type) return false;
    if (status !== "all" && publicRecordStatus(record) !== status) return false;
    if (service !== "all" && !publicRecordServiceIds(record).includes(service)) return false;
    const timestamp = publicRecordTimestamp(record);
    if (fromTime !== null && timestamp < fromTime) return false;
    if (toTime !== null && timestamp > toTime) return false;
    return true;
  });
}

export function buildPublicRecordUrl(record, { origin, history = false } = {}) {
  const type = publicRecordType(record);
  const id = safeText(record?.public_id);
  if (!type || !id) return null;

  const baseOrigin = safeText(origin, "https://status.ivrm.jp").replace(/\/$/, "");
  const path = history ? "/history/" : "/";
  const parameter = type === "incident" ? "incident" : "notice";
  return `${baseOrigin}${path}?${parameter}=${encodeURIComponent(id)}`;
}
