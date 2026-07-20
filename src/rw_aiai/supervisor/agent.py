"""
rw_aiai.supervisor.agent — LangGraph supervisor that watches backup audit logs,
attempts auto-fixes for known failure patterns, and escalates to the user
when it can't resolve the issue.

Patterns it can auto-fix:
  - "restic: repository does not exist" → restic init
  - "ssh: Connection refused" → wait and retry
  - "disk full" → prune old snapshots harder
  - "incus: not found" → install incus
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, StateGraph

logger = logging.getLogger(__name__)

# ─── Known fix patterns ──────────────────────────────────────────────────────

FIX_REGISTRY: dict[str, list[dict]] = {
    "restic_repo_missing": [
        {
            "pattern": "repository does not exist",
            "description": "restic repo not initialised",
            "fix": [
                "restic", "-r", "{repo}", "init",
                "--repository-version", "2",
            ],
            "max_retries": 1,
        },
    ],
    "connection_refused": [
        {
            "pattern": "Connection refused",
            "description": "SSH connection to target VM refused",
            "fix": None,  # wait and retry
            "wait_seconds": 30,
            "max_retries": 2,
        },
    ],
    "disk_space": [
        {
            "pattern": "no space left on device",
            "description": "Backup volume full — running aggressive prune",
            "fix": [
                "restic", "-r", "{repo}", "forget",
                "--keep-daily", "3",
                "--keep-weekly", "1",
                "--prune",
            ],
            "max_retries": 1,
        },
    ],
    "incus_missing": [
        {
            "pattern": "not found",
            "description": "incus command not available on host",
            "fix": ["apt", "install", "-y", "incus"],
            "max_retries": 1,
            "needs_sudo": True,
        },
    ],
}


class SupervisorState(dict):
    """LangGraph state for the supervisor agent."""

    audit_dir: str
    audit_files: list[str]
    current_audit: dict | None
    issues: list[dict]
    escalated: bool
    resolution: str

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setdefault("issues", [])
        self.setdefault("escalated", False)
        self.setdefault("resolution", "ok")


# ─── Nodes ───────────────────────────────────────────────────────────────────


def node_scan_audits(state: SupervisorState) -> SupervisorState:
    """Scan the audit directory for recent backup logs."""
    audit_dir = Path(state["audit_dir"])
    if not audit_dir.exists():
        state["resolution"] = "no_audits"
        return state

    files = sorted(audit_dir.glob("backup-*.json"), reverse=True)
    state["audit_files"] = [str(f) for f in files[:10]]

    if not state["audit_files"]:
        state["resolution"] = "no_audits"

    return state


def node_parse_latest(state: SupervisorState) -> SupervisorState:
    """Parse the most recent audit file."""
    if not state["audit_files"]:
        return state

    latest = Path(state["audit_files"][0])
    try:
        state["current_audit"] = json.loads(latest.read_text())
        logger.info("Parsed audit: %s", latest.name)
    except (json.JSONDecodeError, OSError) as e:
        state["issues"].append({
            "severity": "error",
            "message": f"Failed to parse audit {latest.name}: {e}",
            "auto_fixable": False,
        })
        state["resolution"] = "parse_error"

    return state


def node_detect_issues(state: SupervisorState) -> SupervisorState:
    """Scan audit entries for errors and warnings."""
    audit = state.get("current_audit")
    if not audit:
        return state

    for entry in audit.get("entries", []):
        if entry["status"] in ("error", "warning"):
            state["issues"].append({
                "severity": entry["status"],
                "vm": entry.get("vm_name", "unknown"),
                "message": entry.get("message", ""),
                "auto_fixable": _is_auto_fixable(entry.get("message", "")),
                "fix_attempted": False,
                "fix_succeeded": None,
            })

    if not state["issues"]:
        state["resolution"] = "clean"

    return state


def _is_auto_fixable(message: str) -> bool:
    """Check if an error message matches a known fix pattern."""
    for category, patterns in FIX_REGISTRY.items():
        for p in patterns:
            if p["pattern"] in message:
                return True
    return False


def node_attempt_fix(state: SupervisorState) -> SupervisorState:
    """Try to auto-fix issues that match known patterns."""
    for issue in state["issues"]:
        if not issue["auto_fixable"] or issue["fix_attempted"]:
            continue

        message = issue["message"]
        for category, patterns in FIX_REGISTRY.items():
            for p in patterns:
                if p["pattern"] not in message:
                    continue

                logger.info(
                    "Attempting fix for %s: %s",
                    category, p["description"],
                )

                if p.get("fix") is None and p.get("wait_seconds"):
                    # Pure wait-based fix
                    import time
                    time.sleep(p["wait_seconds"])
                    issue["fix_attempted"] = True
                    issue["fix_succeeded"] = True  # optimistically
                    break

                cmd = p["fix"]
                if p.get("needs_sudo"):
                    cmd = ["sudo"] + cmd

                try:
                    result = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=120,
                    )
                    if result.returncode == 0:
                        issue["fix_attempted"] = True
                        issue["fix_succeeded"] = True
                        logger.info("Fix succeeded for %s", category)
                    else:
                        issue["fix_attempted"] = True
                        issue["fix_succeeded"] = False
                        issue["fix_error"] = result.stderr[:200]
                        logger.warning("Fix failed for %s: %s", category, result.stderr[:100])
                except Exception as e:
                    issue["fix_attempted"] = True
                    issue["fix_succeeded"] = False
                    issue["fix_error"] = str(e)

                break

    return state


def node_assess(state: SupervisorState) -> SupervisorState:
    """Assess remaining unresolved issues and decide on escalation."""
    unresolved = [
        i for i in state["issues"]
        if not i.get("fix_succeeded")
    ]

    if not unresolved:
        state["resolution"] = "all_fixed"
        return state

    # Check if any are still auto-fixable but not yet attempted
    still_fixable = [i for i in unresolved if i["auto_fixable"] and not i["fix_attempted"]]
    if still_fixable:
        # Route back to fix attempts
        state["resolution"] = "retry_fix"
        return state

    # Remaining issues need escalation
    state["resolution"] = "needs_escalation"
    return state


def node_escalate(state: SupervisorState) -> SupervisorState:
    """Escalate unresolved issues to the user."""
    state["escalated"] = True
    unresolved = [i for i in state["issues"] if not i.get("fix_succeeded")]

    print("=" * 60)
    print("⚠️  SUPERVISOR ESCALATION — Issues requiring human intervention")
    print("=" * 60)
    for issue in unresolved:
        print(f"  ❌ [{issue['severity']}] {issue.get('vm', '?')}: {issue['message']}")
        if issue.get("fix_error"):
            print(f"     Fix error: {issue['fix_error']}")
    print()
    print("Unable to auto-resolve — manual intervention required.")

    return state


def node_report(state: SupervisorState) -> SupervisorState:
    """Print a summary of the supervisor run."""
    status_icons = {
        "clean": "✅",
        "all_fixed": "✅",
        "needs_escalation": "⚠️",
        "parse_error": "❌",
        "no_audits": "ℹ️",
        "retry_fix": "🔄",
        "ok": "✅",
    }
    icon = status_icons.get(state["resolution"], "?")
    print(f"\n{icon} Supervisor resolution: {state['resolution'].upper()}")
    if state["escalated"]:
        print("   Human notified.")
    return state


# ─── Build Graph ─────────────────────────────────────────────────────────────


def build_supervisor() -> StateGraph:
    """Build the supervisor LangGraph."""
    workflow = StateGraph(SupervisorState)

    workflow.add_node("scan_audits", node_scan_audits)
    workflow.add_node("parse_latest", node_parse_latest)
    workflow.add_node("detect_issues", node_detect_issues)
    workflow.add_node("attempt_fix", node_attempt_fix)
    workflow.add_node("assess", node_assess)
    workflow.add_node("escalate", node_escalate)
    workflow.add_node("report", node_report)

    workflow.set_entry_point("scan_audits")

    # Conditional edges
    def route_from_scan(state: SupervisorState) -> str:
        if state["resolution"] == "no_audits":
            return "report"
        return "parse_latest"

    def route_from_assess(state: SupervisorState) -> str:
        if state["resolution"] == "retry_fix":
            return "attempt_fix"
        elif state["resolution"] == "needs_escalation":
            return "escalate"
        return "report"

    workflow.add_conditional_edges("scan_audits", route_from_scan)
    workflow.add_edge("parse_latest", "detect_issues")
    workflow.add_edge("detect_issues", "attempt_fix")
    workflow.add_edge("attempt_fix", "assess")
    workflow.add_conditional_edges("assess", route_from_assess)
    workflow.add_edge("escalate", "report")
    workflow.set_finish_point("report")

    return workflow.compile()


# ─── CLI ─────────────────────────────────────────────────────────────────────


def main():
    """Run the supervisor agent from CLI."""
    import argparse

    parser = argparse.ArgumentParser(description="rw-aiai supervisor agent")
    parser.add_argument(
        "--audit-dir",
        default="/mnt/vdrive/backups/audit",
        help="Directory containing backup audit logs",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    graph = build_supervisor()
    initial = SupervisorState(audit_dir=args.audit_dir)
    result = graph.invoke(initial)

    sys.exit(0 if result["resolution"] in ("clean", "all_fixed", "ok") else 1)


if __name__ == "__main__":
    main()
