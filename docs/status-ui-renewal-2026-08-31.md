# Status Portal UI Renewal 2026-08-31

Related: #36 / #27

## Goal

`status.ivrm.jp` を「障害があるかを数秒で判断できる」公開Status Portalとして再整理する。

優先順位は次の通り。

1. Overall status
2. Active Incident / Maintenance
3. Current service status + 24h timeline
4. Upcoming Maintenance
5. Recent Incident / Announcement
6. History

## Public UI principles

- public / read-onlyを維持する。
- Login、管理Mutation、Secretを公開Statusへ持ち込まない。
- 既存Public API / DOM ID / deep-link contractを維持する。
- 24h timelineはDesktop hoverだけでなくkeyboard focus / mobile tapを維持する。
- 状態は色だけで表現せずlabelを併記する。
- 320px幅からdesktopまでhorizontal overflowを発生させない。
- `prefers-reduced-motion` を尊重する。
- CMS feedがstaleでもLive Monitoringと混同しない。

## Visual direction

- 大きすぎるHeroを抑え、overall statusをcompact status boardとして表示する。
- Summaryは4つの独立cardへ整理する。
- Service rowではservice identity、24h timeline、current statusを同じ視線上に配置する。
- Active public recordsだけは通常recordより強いsurfaceで表示する。
- Maintenance / Announcement / HistoryはStatus判断を邪魔しないsecondary hierarchyにする。
- decorative glow / orbitは状態判定の邪魔にならない強度へ抑える。

## CMS architecture

```text
admin.ivrm.jp / apps/admin
  ↓ Discord OAuth / RBAC
api.ivrm.jp
  ↓ server-side only
Supabase ivrm-core
  ├─ status_incidents
  ├─ status_incident_updates
  ├─ status_maintenance_notices
  ├─ status_announcements
  ├─ status mutation RPC
  └─ get_status_public_feed_v1
        ↓ sanitized public feed
OCI ivrm-status Status API
  ├─ live monitoring
  ├─ public CMS cache
  └─ last-known-good
        ↓
status.ivrm.jp
```

## CMS decision

新しいCMS製品や別DBは導入しない。

Supabase `ivrm-core` に既存のStatus authoring model / lifecycle RPCが存在するため、これをContent Source of Truthとして維持する。

将来のauthoring UIは `ivRooom/ivrm-web/apps/admin` の `admin.ivrm.jp` へ統合する。BrowserからSupabase service roleを使用せず、`api.ivrm.jp` の認証済みserver-side boundaryを介する。

既存 `console.ivrm.jp` Status Centerは移行元として扱い、admin移行時にparallel writeは行わない。

## Admin CMS minimum UX

### Incident

- draft create
- publish
- investigating / identified / monitoring / resolved update
- impact
- affected services
- public summary
- append-only update history

### Maintenance

- draft
- scheduled publish
- starts / ends
- affected services
- cancel
- optional reliability window relation

### Announcement

- info / warning
- draft
- scheduled publish
- expiry
- archive

### Required platform controls

- existing Discord session
- RBAC / fine-grained policy
- origin / CSRF protection
- idempotency
- audit log
- plain-text first for public body
- service role / webhook secrets are server-side only

## Migration order

1. Public UI renewal in `ivrm-status`.
2. `ivrm-web#295` admin Status CMS read model / IA.
3. `api.ivrm.jp` Status admin endpoints backed by existing Supabase RPC.
4. Incident / Maintenance / Announcement authoring UI.
5. E2E: admin create → publish → public feed → `status.ivrm.jp`.
6. Operational cutover from console to admin; no dual write.
