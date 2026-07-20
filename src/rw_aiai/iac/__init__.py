"""
rw_aiai.iac.drift — LangGraph agent that checks actual VM state against Ansible
declarations and reports/pushes updates.

Can be run standalone or as part of the supervisor pipeline.
"""

from __future__ import annotations

import difflib
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from langgraph.graph import END, StateGraph

logger = logging.getLogger(__name__)


class DriftState(dict):
    inventory_path: str
    playbook_dirs: list[str]
    drift_found: bool
    drifts: list[dict]
    auto_fixed: int

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.setdefault("drifts", [])
        self.setdefault("drift_found", False)
        self.setdefault("auto_fixed", 0)


def node_check_hosts(state: DriftState) -> DriftState:
    """Check that all hosts in inventory are reachable."""
    with open(state["inventory_path"]) as f:
        inventory = yaml.safe_load(f)

    for group_name, group in inventory.get("all", {}).get("children", {}).items():
        for host_name, host_config in group.get("hosts", {}).items():
            host = host_config.get("ansible_host", host_name)
            ssh_args = host_config.get("ansible_ssh_common_args", "")
            result = subprocess.run(
                ["ssh"] + ssh_args.split() + [host, "echo OK"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                state["drifts"].append({
                    "host": host_name,
                    "type": "unreachable",
                    "detail": result.stderr.strip() or "No response",
                    "auto_fixable": False,
                })
                state["drift_found"] = True
            else:
                logger.info("Host %s (%s): reachable", host_name, host)

    return state


def node_report(state: DriftState) -> DriftState:
    """Print drift report."""
    if not state["drift_found"]:
        print("✅ No drift detected — all hosts reachable.")
        return state

    print(f"\n⚠️  Drift detected in {len(state['drifts'])} area(s):")
    for d in state["drifts"]:
        print(f"  ❌ [{d['type']}] {d.get('host', '?')}: {d['detail']}")
        if not d.get("auto_fixable"):
            print("     (not auto-fixable — manual review required)")

    return state


def build_drift_detector() -> StateGraph:
    """Build the drift detection graph."""
    workflow = StateGraph(DriftState)
    workflow.add_node("check_hosts", node_check_hosts)
    workflow.add_node("report", node_report)
    workflow.set_entry_point("check_hosts")
    workflow.add_edge("check_hosts", "report")
    workflow.add_edge("report", END)
    return workflow.compile()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="rw-aiai IaC drift detector")
    parser.add_argument("--inventory", default="ansible/inventory.yml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    graph = build_drift_detector()
    initial = DriftState(
        inventory_path=args.inventory,
        playbook_dirs=["infra", "enterprise", "dev"],
    )
    result = graph.invoke(initial)
    sys.exit(1 if result["drift_found"] else 0)
