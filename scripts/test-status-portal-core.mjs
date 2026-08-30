import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import {
  buildPublicRecordUrl,
  classifyPublicId,
  combineArchiveRecords,
  filterArchiveRecords,
  normalizePublicCollections,
  normalizeTimelineDetails,
  parsePortalDeepLink,
} from "../assets/status-portal-core.js";

const incident = {
  public_id: "INC-ABCDEF123456",
  title: "Minecraft incident",
  status: "resolved",
  impact: "major",
  affected_service_ids: ["minecraft-network"],
  started_at: "2026-08-28T01:00:00Z",
  resolved_at: "2026-08-28T02:00:00Z",
};
const maintenance = {
  public_id: "MNT-ABCDEF123456",
  title: "Maintenance",
  state: "scheduled",
  affected_service_ids: ["herta-discord-bot"],
  starts_at: "2026-08-30T03:00:00Z",
  ends_at: "2026-08-30T04:00:00Z",
};
const announcement = {
  public_id: "ANN-ABCDEF123456",
  title: "Announcement",
  kind: "warning",
  affected_service_ids: [],
  published_at: "2026-08-29T03:00:00Z",
};

assert.equal(classifyPublicId(incident.public_id), "incident");
assert.equal(classifyPublicId(maintenance.public_id), "maintenance");
assert.equal(classifyPublicId(announcement.public_id), "announcement");
assert.equal(classifyPublicId("8d4a08e9-bc41-4a43-9f18-79c4a7c7b991"), null);
assert.equal(classifyPublicId("INC-../../passwd"), null);

assert.deepEqual(parsePortalDeepLink("?incident=INC-ABCDEF123456"), {
  kind: "incident",
  id: "INC-ABCDEF123456",
  valid: true,
});
assert.equal(parsePortalDeepLink("?incident=bad")?.valid, false);
assert.deepEqual(parsePortalDeepLink("?notice=MNT-ABCDEF123456"), {
  kind: "maintenance",
  id: "MNT-ABCDEF123456",
  valid: true,
});
assert.deepEqual(parsePortalDeepLink("?notice=ANN-ABCDEF123456"), {
  kind: "announcement",
  id: "ANN-ABCDEF123456",
  valid: true,
});
assert.equal(parsePortalDeepLink("?notice=NOTICE-ABCDEF123456")?.valid, false);

const empty = normalizePublicCollections({ services: [] });
assert.deepEqual(empty.incidents, []);
assert.deepEqual(empty.maintenance, []);
assert.deepEqual(empty.announcements, []);

const details = normalizeTimelineDetails({
  timeline_details: [{
    start_at: "2026-08-30T01:00:00Z",
    end_at: "2026-08-30T02:00:00Z",
    status: "degraded",
    related_incident_ids: ["INC-ABCDEF123456", "internal-uuid"],
    summary: "Published reason",
  }],
});
assert.equal(details.length, 24);
assert.equal(details.at(-1).status, "degraded");
assert.deepEqual(details.at(-1).related_incident_ids, ["INC-ABCDEF123456"]);

const archive = combineArchiveRecords({
  incidents: [incident],
  maintenance: [maintenance],
  announcements: [announcement],
});
assert.equal(archive.length, 3);
assert.equal(archive[0].public_id, maintenance.public_id);
assert.equal(filterArchiveRecords(archive, { type: "incident" }).length, 1);
assert.equal(filterArchiveRecords(archive, { status: "warning" }).length, 1);
assert.equal(filterArchiveRecords(archive, { service: "minecraft-network" }).length, 1);
assert.equal(filterArchiveRecords(archive, { fromDate: "2026-08-30", toDate: "2026-08-30" }).length, 1);
assert.equal(filterArchiveRecords(archive, { fromDate: "2026-08-27", toDate: "2026-08-28" }).length, 1);

const incidentUrl = buildPublicRecordUrl(incident, { origin: "https://status.ivrm.jp", history: false });
const maintenanceUrl = buildPublicRecordUrl(maintenance, { origin: "https://status.ivrm.jp", history: true });
assert.equal(incidentUrl, "https://status.ivrm.jp/?incident=INC-ABCDEF123456");
assert.equal(maintenanceUrl, "https://status.ivrm.jp/history/?notice=MNT-ABCDEF123456");
assert.equal(incidentUrl.includes("8d4a08e9"), false);

const [homeSource, historySource] = await Promise.all([
  readFile(new URL("../assets/status-presentation.js", import.meta.url), "utf8"),
  readFile(new URL("../assets/history-portal.js", import.meta.url), "utf8"),
]);
for (const source of [homeSource, historySource]) {
  for (const sink of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "setHTMLUnsafe"]) {
    assert.equal(source.includes(sink), false, `public CMS rendering must not use ${sink}`);
  }
  assert.match(source, /\.textContent\s*=/, "public CMS rendering must assign untrusted copy through textContent");
  assert.match(source, /execCommand\(\s*["']copy["']\s*\)/, "clipboard fallback must remain available");
  assert.match(source, /navigator\.share/, "Web Share support must remain available when supported");
}

console.log("Status Portal core tests passed.");