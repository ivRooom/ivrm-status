"""Seventh round: an incident under maintenance, and the order of the installation steps."""

from __future__ import annotations

import re

from ops_agent.watch import concerns

from test_deploy_files import ROOT  # noqa: F401
from test_watch import Analyzer, Notifier, T0, cycle, snap, store  # noqa: F401

MAINTENANCE_MC = [{"state": "in_progress", "affected_service_ids": ["minecraft-network"]}]


def incident(*, affected, status="investigating", public_id="INC-0123456789AB", title="Minecraft 接続障害"):
    return {"public_id": public_id, "title": title, "status": status, "affected_service_ids": affected}


def snapshot_with(*incidents, maintenance=None):
    snapshot = snap(maintenance=maintenance)
    snapshot["status"]["incidents"] = list(incidents)
    return snapshot


# --- an incident whose services are all under maintenance --------------------------------------------


def test_an_incident_covered_by_a_maintenance_is_not_a_concern() -> None:
    found = concerns(snapshot_with(incident(affected=["minecraft-network"]), maintenance=MAINTENANCE_MC)["status"])
    assert found.services == [] and "incident:INC-0123456789AB" in found.suppressed_ids


def test_an_incident_is_reported_while_any_of_its_services_is_not_covered() -> None:
    both = incident(affected=["minecraft-network", "herta-discord-bot"])
    found = concerns(snapshot_with(both, maintenance=MAINTENANCE_MC)["status"])
    assert found.services == ["Incident: Minecraft 接続障害"]


def test_an_incident_without_affected_services_is_still_reported() -> None:
    for affected in (None, [], "minecraft-network"):
        found = concerns(snapshot_with(incident(affected=affected), maintenance=MAINTENANCE_MC)["status"])
        assert found.services == ["Incident: Minecraft 接続障害"], affected


def test_an_incident_is_reported_when_the_maintenance_is_only_scheduled() -> None:
    scheduled = [{"state": "scheduled", "affected_service_ids": ["minecraft-network"]}]
    found = concerns(snapshot_with(incident(affected=["minecraft-network"]), maintenance=scheduled)["status"])
    assert found.services == ["Incident: Minecraft 接続障害"]


def test_a_covered_incident_never_starts_a_debounce_or_an_analysis(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    covered = snapshot_with(incident(affected=["minecraft-network"]), maintenance=MAINTENANCE_MC)
    assert cycle(store, notifier, analyzer, covered, T0).action == "healthy"
    assert cycle(store, notifier, analyzer, covered, T0 + 600).action == "healthy"
    assert notifier.sent == [] and analyzer.calls == 0


def test_a_notified_incident_that_gets_covered_is_not_announced_as_recovered(store) -> None:
    notifier, analyzer = Notifier(), Analyzer()
    open_incident = snapshot_with(incident(affected=["minecraft-network"]))
    cycle(store, notifier, analyzer, open_incident, T0)
    cycle(store, notifier, analyzer, open_incident, T0 + 300)
    assert len(notifier.sent) == 1
    covered = snapshot_with(incident(affected=["minecraft-network"]), maintenance=MAINTENANCE_MC)
    assert cycle(store, notifier, analyzer, covered, T0 + 400).action == "suppressed_by_maintenance"
    assert len(notifier.sent) == 1 and all("回復" not in message for message in notifier.sent)


# --- the installation steps can be followed in order --------------------------------------------------


def numbered_steps(text: str) -> dict[int, str]:
    """The numbered steps of the installation section, keyed by their number."""
    section = text[text.index("## 導入") :]
    pieces = re.split(r"(?m)^(\d+)\. ", section)
    return {int(pieces[i]): pieces[i + 1] for i in range(1, len(pieces) - 1, 2)}


def test_the_files_are_placed_before_the_step_that_installs_from_them() -> None:
    steps = numbered_steps((ROOT / "DEPLOY.md").read_text(encoding="utf-8"))
    placing = next(n for n, body in steps.items() if "/opt/ivrm-ops-agent/requirements.txt  " in body)
    installing = next(n for n, body in steps.items() if "pip install -r /opt/ivrm-ops-agent/requirements.txt" in body)
    assert placing < installing


def test_every_path_a_step_reads_has_been_created_by_an_earlier_step() -> None:
    steps = numbered_steps((ROOT / "DEPLOY.md").read_text(encoding="utf-8"))
    created: set[str] = set()
    for number in sorted(steps):
        body = steps[number]
        for needed in re.findall(r"(?:install -r|-f|EnvironmentFile=|\.\./)(/opt/ivrm-ops-agent/[\w./-]+)", body):
            assert any(needed.startswith(path) or path.startswith(needed) for path in created), (number, needed)
        created.update(re.findall(r"^\s+(/opt/ivrm-ops-agent/[\w./]+)", body, re.M))
        created.update(re.findall(r"python3\.11 -m venv (/opt/ivrm-ops-agent/[\w./]+)", body))
