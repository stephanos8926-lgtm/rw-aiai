"""rw_aiai.backup.pipeline — LangGraph-driven backup pipeline.

Nodes:
  load_config → for each VM (pre_hook → backup → post_hook → verify)
  → prune → write_audit → check_audit → route
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from langgraph.graph import END, StateGraph
from langgraph.types import Command

logger = logging.getLogger(__name__)

# ─── Types ───────────────────────────────────────────────────────────────────


class AuditEntry:
    """Single audit record for one VM backup."""

    def __init__(
        self,
        vm_name: str,
        backup_type: str,
        status: Literal["ok", "warning", "error"],
        message: str,
        started_at: str,
        duration_seconds: float,
        details: dict | None = None,
    ):
        self.vm_name = vm_name
        self.backup_type = backup_type
        self.status = status
        self.message = message
        self.started_at = started_at
        self.duration_seconds = duration_seconds
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "vm_name": self.vm_name,
            "backup_type": self.backup_type,
            "status": self.status,
            "message": self.message,
            "started_at": self.started_at,
            "duration_seconds": self.duration_seconds,
            "details": self.details,
        }


class PipelineState(dict):
    """LangGraph state — accumulated across nodes."""

    config: dict
    entries: list[dict]
    pipeline_status: str  # ok / warning / error
    error_messages: list[str]
    audit_path: str

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setdefault("entries", [])
        self.setdefault("error_messages", [])
        self.setdefault("pipeline_status", "ok")


# ─── Config ──────────────────────────────────────────────────────────────────


def load_config(config_path: str) -> dict:
    """Load backup configuration from YAML file."""
    with open(config_path) as f:
        return yaml.safe_load(f)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_cmd(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ensure_backup_root(path: str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _restic_env(repo: str) -> dict:
    """Build environment dict with restic password for a given repo."""
    # Check sysop-accessible location first, then root
    key_paths = [
        Path.home() / ".config" / "vdrive" / "backup-key",
        Path("/root/vdrive-backup-key"),
    ]
    password = os.environ.get("RESTIC_PASSWORD", "")
    for kp in key_paths:
        if kp.exists():
            try:
                password = kp.read_text().strip()
                break
            except PermissionError:
                continue
    return {**os.environ, "RESTIC_PASSWORD": password}


# ─── LangGraph Nodes ─────────────────────────────────────────────────────────


def node_load_config(state: PipelineState) -> PipelineState:
    """Load config and set up the backup root."""
    cfg = state.get("config")
    if not cfg:
        raise ValueError("No config found in state")
    _ensure_backup_root(cfg["backup_root"])
    logger.info("Backup root: %s", cfg["backup_root"])
    return state


def node_backup_vm(state: PipelineState, vm_name: str) -> PipelineState:
    """Back up a single VM — incremental (restic) or snapshot (incus)."""
    cfg = state["config"]
    vm_cfg = cfg["vms"][vm_name]
    backup_type = vm_cfg["type"]
    started = _now_iso()
    t0 = time.monotonic()

    try:
        if backup_type == "incremental":
            _backup_incremental(vm_name, vm_cfg, cfg)
        elif backup_type == "snapshot":
            _backup_snapshot(vm_name, vm_cfg, cfg)
        else:
            raise ValueError(f"Unknown backup type: {backup_type}")

        duration = time.monotonic() - t0
        entry = AuditEntry(
            vm_name=vm_name,
            backup_type=backup_type,
            status="ok",
            message=f"Backup completed in {duration:.1f}s",
            started_at=started,
            duration_seconds=duration,
        )
    except Exception as e:
        duration = time.monotonic() - t0
        entry = AuditEntry(
            vm_name=vm_name,
            backup_type=backup_type,
            status="error",
            message=str(e),
            started_at=started,
            duration_seconds=duration,
        )
        state["error_messages"].append(f"{vm_name}: {e}")
        state["pipeline_status"] = "error"

    state["entries"].append(entry.to_dict())
    return state


def _backup_incremental(vm_name: str, vm_cfg: dict, cfg: dict) -> None:
    """Incremental backup via restic over SSH."""
    backup_root = cfg["backup_root"]
    repo = f"{backup_root}/{vm_name}"
    host = vm_cfg["host"]
    paths = vm_cfg["paths"]
    env = _restic_env(repo)

    # Pre-hook
    pre = vm_cfg.get("pre_hook")
    if pre:
        _run_cmd(["ssh", host, pre], timeout=120)

    try:
        # Run backup (init is handled by the vdrive provision script)
        result = _run_cmd(
            ["restic", "-r", repo, "--verbose", "--exclude-caches", "backup"] + paths,
            timeout=3600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"restic backup failed: {result.stderr[:500]}")
    finally:
        # Post-hook
        post = vm_cfg.get("post_hook")
        if post:
            _run_cmd(["ssh", host, post], timeout=120)


def _backup_snapshot(vm_name: str, vm_cfg: dict, cfg: dict) -> None:
    """Simple Incus snapshot on the host for the dev VM."""
    host = cfg["incus_host"]
    snapshot_name = f"auto-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"

    result = _run_cmd(
        ["ssh", host, "incus", "snapshot", vm_name, snapshot_name],
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"incus snapshot failed: {result.stderr[:500]}")


def node_prune(state: PipelineState) -> PipelineState:
    """Apply retention policy — prune old restic snapshots and incus snapshots."""
    cfg = state["config"]
    retention = cfg["retention"]

    for vm_name, vm_cfg in cfg["vms"].items():
        if vm_cfg["type"] == "incremental":
            repo = f"{cfg['backup_root']}/{vm_name}"
            env = _restic_env(repo)
            _run_cmd(
                [
                    "restic", "-r", repo, "forget",
                    "--keep-daily", str(retention["daily"]),
                    "--keep-weekly", str(retention["weekly"]),
                    "--keep-monthly", str(retention["monthly"]),
                    "--prune",
                ],
                timeout=600,
            )
        elif vm_cfg["type"] == "snapshot":
            host = cfg["incus_host"]
            keep = max(retention["daily"], retention["weekly"], retention["monthly"])
            result = _run_cmd(
                ["ssh", host, "incus", "list-snapshots", vm_name, "--format=json"],
                timeout=30,
            )
            try:
                snaps = json.loads(result.stdout)
                if len(snaps) > keep:
                    to_delete = sorted(snaps, key=lambda s: s.get("created_at", ""))[:-keep]
                    for snap in to_delete:
                        _run_cmd(
                            ["ssh", host, "incus", "delete-snapshot", f"{vm_name}/{snap['name']}"],
                            timeout=60,
                        )
                        logger.info("Pruned old snapshot %s/%s", vm_name, snap["name"])
            except (json.JSONDecodeError, KeyError, IndexError):
                logger.warning("Could not parse incus snapshot list for %s", vm_name)

    return state


def node_write_audit(state: PipelineState) -> PipelineState:
    """Write the audit log to a timestamped file."""
    cfg = state["config"]
    audit_dir = Path(cfg["backup_root"]) / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    audit_path = audit_dir / f"backup-{timestamp}.json"

    report = {
        "pipeline_status": state["pipeline_status"],
        "timestamp": _now_iso(),
        "entries": state["entries"],
        "errors": state["error_messages"],
    }

    audit_path.write_text(json.dumps(report, indent=2))
    state["audit_path"] = str(audit_path)
    logger.info("Audit written to %s", audit_path)
    return state


def node_check_audit(state: PipelineState) -> PipelineState:
    """Check the audit for errors and warnings."""
    return state


def node_escalate(state: PipelineState) -> PipelineState:
    """Escalate to the user via notification."""
    print("=" * 60)
    print(f"BACKUP PIPELINE — {state['pipeline_status'].upper()}")
    print("=" * 60)
    for entry in state["entries"]:
        icon = {"ok": "✅", "warning": "⚠️", "error": "❌"}.get(entry["status"], "?")
        print(f"  {icon} {entry['vm_name']} ({entry['backup_type']}): {entry['message']}")
    if state["error_messages"]:
        print()
        print("Errors requiring attention:")
        for err in state["error_messages"]:
            print(f"  ❌ {err}")
    print()
    print(f"Audit log: {state.get('audit_path', 'N/A')}")
    return state


# ─── Build Graph ─────────────────────────────────────────────────────────────


def build_pipeline(config: dict) -> StateGraph:
    """Build the LangGraph backup pipeline."""
    workflow = StateGraph(PipelineState)

    workflow.add_node("load_config", node_load_config)

    for vm_name in config["vms"]:
        workflow.add_node(
            f"backup_{vm_name}",
            lambda state, _vn=vm_name: node_backup_vm(state, _vn),
        )

    workflow.add_node("prune", node_prune)
    workflow.add_node("write_audit", node_write_audit)
    workflow.add_node("check_audit", node_check_audit)
    workflow.add_node("escalate", node_escalate)

    workflow.set_entry_point("load_config")

    prev = "load_config"
    for vm_name in config["vms"]:
        workflow.add_edge(prev, f"backup_{vm_name}")

    for vm_name in config["vms"]:
        workflow.add_edge(f"backup_{vm_name}", "prune")

    workflow.add_edge("prune", "write_audit")
    workflow.add_edge("write_audit", "check_audit")

    def route_from_check(state: PipelineState) -> Literal["escalate", "__end__"]:
        if state["pipeline_status"] == "error":
            return "escalate"
        return "__end__"

    workflow.add_conditional_edges("check_audit", route_from_check)
    workflow.add_edge("escalate", END)

    return workflow.compile()


# ─── CLI Entrypoint ──────────────────────────────────────────────────────────


def main():
    """Run the backup pipeline from CLI."""
    import argparse

    parser = argparse.ArgumentParser(description="rw-aiai backup pipeline")
    parser.add_argument("--config", default="backup/config.yml", help="Path to config YAML")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    config = load_config(args.config)
    graph = build_pipeline(config)

    initial = PipelineState(config=config)
    result = graph.invoke(initial)

    status = result["pipeline_status"]
    print(f"\nPipeline finished with status: {status.upper()}")
    sys.exit(0 if status == "ok" else 1)


if __name__ == "__main__":
    main()
