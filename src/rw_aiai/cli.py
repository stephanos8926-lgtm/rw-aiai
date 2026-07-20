"""
rwa — RapidWebs AI Agent CLI

Unified command-line interface for backup, restore, and infrastructure management.

Auto-detects whether it runs on the host (srv1) or workstation. Commands that need
the vdrive (backup, restore) transparently SSH to srv1 when run from the workstation.

Usage:
    rwa backup run [--vm VM] [--config CONFIG] [--host HOST]
    rwa backup status [--host HOST]
    rwa restore list [<vm>] [--host HOST]
    rwa restore latest <vm> [--target PATH] [--host HOST]
    rwa restore snapshot <vm> <snapshot-id> [--target PATH] [--host HOST]
    rwa playbook list
    rwa playbook run <vm> [--check]
    rwa status
    rwa sync [--target HOST]
    rwa snapshot <vm>
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import yaml

logger = logging.getLogger(__name__)

# ─── Paths ───────────────────────────────────────────────────────────────────

HERE = Path(__file__).parent.resolve()
PROJECT_ROOT = HERE.parent.parent  # src/rw_aiai/ → src/ → rw_aiai/
ANSIBLE_DIR = PROJECT_ROOT / "ansible"
BACKUP_CONFIG = PROJECT_ROOT / "backup" / "config.yml"
BACKUP_HOST = os.environ.get("RWA_HOST", "srv1")

# ─── Helpers ─────────────────────────────────────────────────────────────────


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)


def ok(msg: str):
    print(f"✅ {msg}")


def warn(msg: str):
    print(f"⚠️  {msg}")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, return result."""
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    return result


def ssh_run(host: str, cmd: str) -> subprocess.CompletedProcess:
    """Run a command on a remote host via SSH."""
    return run(["ssh", host, cmd])


def _is_host() -> bool:
    """Detect if we're running on the backup host (where vdrive lives)."""
    try:
        result = run(["mountpoint", "-q", "/mnt/vdrive"])
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _load_backup_config() -> dict:
    if not BACKUP_CONFIG.exists():
        die(f"Backup config not found: {BACKUP_CONFIG}")
    return yaml.safe_load(BACKUP_CONFIG.read_text())


def _restic_cmd(host: str, vm: str, action: str, *args: str) -> list[str]:
    """Build a restic command — runs locally or via SSH."""
    repo = f"/mnt/vdrive/backups/{vm}"
    pw_cmd = "export RESTIC_PASSWORD=$(cat ~/.config/vdrive/backup-key 2>/dev/null || sudo cat /root/vdrive-backup-key 2>/dev/null || echo '')"
    if host and host != "local":
        cmd = f"{pw_cmd} && restic -r {repo} {action} " + " ".join(shq(a) for a in args)
        return ["ssh", host, cmd]
    else:
        env = {**os.environ}
        key = Path("/root/vdrive-backup-key")
        if key.exists():
            env["RESTIC_PASSWORD"] = key.read_text().strip()
        return ["restic", "-r", repo, action, *args]


def shq(s: str) -> str:
    """Shell-quote a string for SSH commands."""
    escaped = s.replace("'", "'\\''")
    return f"'{escaped}'"


def _resolve_host(args: argparse.Namespace) -> str:
    """Return the host to use for backup/restore operations."""
    if hasattr(args, "host") and args.host:
        return args.host
    return "local" if _is_host() else BACKUP_HOST


# ─── Backup subcommands ──────────────────────────────────────────────────────


def cmd_backup_run(args: argparse.Namespace):
    """Run the backup pipeline for one or all VMs."""
    host = _resolve_host(args)

    if host != "local":
        # Run remotely via SSH
        rwa_path = "cd /home/sysop/Workspaces/rw-aiai && PYTHONPATH=src python3 -m rw_aiai.cli"
        vm_arg = f"--vm {args.vm}" if args.vm else ""
        remote_cmd = f"{rwa_path} backup run {vm_arg} --host local"
        print(f"Running backup on {host}...")
        result = run(["ssh", host, remote_cmd])
        print(result.stdout)
        if result.returncode != 0:
            die(f"Remote backup failed: {result.stderr[:300]}")
        return

    from rw_aiai.backup.pipeline import build_pipeline, PipelineState, load_config

    config = load_config(str(BACKUP_CONFIG))
    if args.vm:
        if args.vm not in config["vms"]:
            die(f"Unknown VM '{args.vm}'. Known: {', '.join(config['vms'])}")
        config["vms"] = {args.vm: config["vms"][args.vm]}

    graph = build_pipeline(config)
    state = PipelineState(config=config)
    result = graph.invoke(state)

    for entry in result["entries"]:
        icon = {"ok": "✅", "warning": "⚠️", "error": "❌"}.get(entry["status"], "?")
        print(f"  {icon} {entry['vm_name']}: {entry['message']}")

    if result["pipeline_status"] == "ok":
        ok(f"Backup complete — {len(result['entries'])} VM(s) backed up")
    else:
        die(f"Backup pipeline finished with status: {result['pipeline_status'].upper()}")


def cmd_backup_status(args: argparse.Namespace):
    """Show backup status — latest snapshot info per VM."""
    host = _resolve_host(args)

    if host != "local":
        rwa_path = "cd /home/sysop/Workspaces/rw-aiai && PYTHONPATH=src python3 -m rw_aiai.cli"
        result = run(["ssh", host, f"{rwa_path} backup status --host local"])
        print(result.stdout)
        return

    mount = "/mnt/vdrive"
    if not Path(mount).is_mount():
        die("Vdrive not mounted at /mnt/vdrive")

    config = _load_backup_config()
    print("=== Backup Status ===")
    for vm_name in config["vms"]:
        repo = f"{mount}/backups/{vm_name}"
        if not Path(repo, "config").exists():
            warn(f"{vm_name}: restic repo not initialised")
            continue
        result = run(_restic_cmd("local", vm_name, "snapshots", "--compact"))
        if result.returncode == 0:
            lines = [l for l in result.stdout.strip().split("\n")
                     if l and "ID" not in l and not l.startswith("---")
                     and not l.startswith("repository")]
            if lines:
                print(f"  {vm_name}: {lines[-1].strip()}")
            else:
                warn(f"{vm_name}: no snapshots")
        else:
            warn(f"{vm_name}: {result.stderr[:100]}")
    print()


# ─── Restore subcommands ─────────────────────────────────────────────────────


def _restore_cmd(args: argparse.Namespace, snapshot_ref: str):
    """Execute a restore (latest or snapshot-id)."""
    host = _resolve_host(args)
    target = args.target or f"/mnt/restore/{args.vm}"

    if host != "local":
        rwa_path = "cd /home/sysop/Workspaces/rw-aiai && PYTHONPATH=src python3 -m rw_aiai.cli"
        target_flag = f"--target {target}" if args.target else ""
        sub = "latest" if snapshot_ref == "latest" else f"snapshot {snapshot_ref}"
        remote_cmd = f"{rwa_path} restore {sub} {args.vm} {target_flag} --host local"
        print(f"Running restore on {host}...")
        result = run(["ssh", host, remote_cmd])
        print(result.stdout)
        if result.returncode != 0:
            die(f"Remote restore failed: {result.stderr[:300]}")
        return

    print(f"Restoring {args.vm} {snapshot_ref} → {target}")
    result = run(_restic_cmd("local", args.vm, "restore", snapshot_ref, "--target", target))
    if result.returncode == 0:
        ok(f"{args.vm} restored to {target}")
    else:
        die(f"Restore failed: {result.stderr[:500]}")


def cmd_restore_list(args: argparse.Namespace):
    """List available snapshots for one or all VMs."""
    host = _resolve_host(args)

    if host != "local":
        rwa_path = "cd /home/sysop/Workspaces/rw-aiai && PYTHONPATH=src python3 -m rw_aiai.cli"
        vm_arg = args.vm or ""
        result = run(["ssh", host, f"{rwa_path} restore list {vm_arg} --host local"])
        print(result.stdout)
        return

    mount = "/mnt/vdrive"
    vms = [args.vm] if args.vm else list(_load_backup_config()["vms"])

    for vm in vms:
        repo = f"{mount}/backups/{vm}"
        if not Path(repo, "config").exists():
            warn(f"{vm}: repo not found")
            continue
        result = run(_restic_cmd("local", vm, "snapshots"))
        if result.returncode == 0:
            print(f"\n=== {vm} ===")
            print(result.stdout.strip())
        else:
            warn(f"{vm}: {result.stderr[:100]}")


def cmd_restore_latest(args: argparse.Namespace):
    """Restore the latest snapshot for a VM."""
    _restore_cmd(args, "latest")


def cmd_restore_snapshot(args: argparse.Namespace):
    """Restore a specific snapshot by ID."""
    _restore_cmd(args, args.snapshot_id)


# ─── Playbook subcommands ────────────────────────────────────────────────────


def cmd_playbook_list(args: argparse.Namespace):
    """List available Ansible playbooks."""
    playbooks = sorted(ANSIBLE_DIR.glob("*/playbook.yml"))
    if not playbooks:
        die(f"No playbooks found in {ANSIBLE_DIR}")

    print("=== Available Playbooks ===")
    for pb in playbooks:
        vm_name = pb.parent.name
        with open(pb) as f:
            first_line = f.readline().strip().lstrip("# -").strip()
            desc = first_line[:80] if first_line else ""
        print(f"  {vm_name:<15} {desc}")
    print()
    print("Run:  rwa playbook run <vm> [--check]")


def cmd_playbook_run(args: argparse.Namespace):
    """Run an Ansible playbook for a VM."""
    playbook = ANSIBLE_DIR / args.vm / "playbook.yml"
    if not playbook.exists():
        die(f"Playbook not found: {playbook}")

    inventory = ANSIBLE_DIR / "inventory.yml"
    if not inventory.exists():
        die(f"Inventory not found: {inventory}")

    cmd = ["ansible-playbook", "-i", str(inventory), str(playbook)]
    if args.check:
        cmd.append("--check")

    print(f"Running: {' '.join(cmd)}")
    print()
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


# ─── Status / Sync / Snapshot ────────────────────────────────────────────────


def cmd_status(args: argparse.Namespace):
    """Overall health snapshot — vdrive, backups, playbooks."""
    print("=== rwa status ===")
    print()

    # Vdrive — try local first, then remote
    if _is_host():
        ok("Vdrive mounted locally")
        result = run(["df", "-h", "/mnt/vdrive"])
        for line in result.stdout.strip().split("\n"):
            if "loop" in line or "Filesystem" in line:
                print(f"  {line}")

        config = _load_backup_config()
        for vm_name in config["vms"]:
            repo = f"/mnt/vdrive/backups/{vm_name}"
            if Path(repo, "config").exists():
                result = run(_restic_cmd("local", vm_name, "snapshots", "--compact"))
                count = max(0, result.stdout.count("\n") - 3)
                ok(f"{vm_name}: {count} snapshot(s)")
            else:
                warn(f"{vm_name}: repo not initialised")
    else:
        warn("Vdrive not mounted locally")
        print("  (run `rwa backup status --host srv1` for remote status)")
        print()

    # Playbooks
    playbooks = sorted(ANSIBLE_DIR.glob("*/playbook.yml"))
    print(f"\nPlaybooks: {len(playbooks)} available")
    for pb in playbooks:
        print(f"  {pb.parent.name}")

    inventory = ANSIBLE_DIR / "inventory.yml"
    if inventory.exists():
        ok(f"Inventory: present ({len(inventory.read_text().splitlines())} lines)")

    # Workstation sync slots
    slots = Path("/mnt/remote_backups")
    if slots.exists():
        print(f"\nWorkstation backup slots:")
        for s in sorted(slots.glob("slot_*")):
            img = s / "backup.img"
            if img.exists():
                actual = run(["du", "-h", str(img)]).stdout.strip().split()[0]
                print(f"  {s.name}: {actual}")
            else:
                print(f"  {s.name}: (empty)")

    print()


def cmd_sync(args: argparse.Namespace):
    """Sync vdrive image — push from host or pull to workstation."""
    mount = "/mnt/vdrive"
    local_vdrive = Path("/vdrive/backup.img")

    if local_vdrive.exists():
        # We're on the host — push
        target = args.target or "sysop@rw-workstation-01:/mnt/remote_backups"
        print(f"Pushing backup.img → {target}...")
        result = run(["rsync", "-avz", "--sparse", "-e", "ssh", str(local_vdrive), target])
        if result.returncode == 0:
            ok("Push complete")
        else:
            die(f"Push failed: {result.stderr[:200]}")
    elif Path("/mnt/remote_backups").exists():
        # We're on the workstation — pull
        source = args.target or f"sysop@{BACKUP_HOST}:/vdrive/backup.img"
        slot_1 = "/mnt/remote_backups/slot_1"
        slot_2 = "/mnt/remote_backups/slot_2"
        incoming = f"{slot_1}/backup.img.incoming"

        Path(slot_1).mkdir(parents=True, exist_ok=True)
        Path(slot_2).mkdir(parents=True, exist_ok=True)

        print(f"Pulling {source} → {incoming}...")
        result = run(["rsync", "-avz", "--sparse", "-e", "ssh", source, incoming])
        if result.returncode != 0:
            die(f"Pull failed: {result.stderr[:200]}")

        current = Path(f"{slot_1}/backup.img")
        if current.exists():
            print("  Rotating: slot_1 → slot_2")
            current.rename(f"{slot_2}/backup.img")

        Path(incoming).rename(current)
        actual = run(["du", "-h", str(current)]).stdout.strip().split()[0]
        ok(f"Sync complete — slot_1: {actual}")
    else:
        die("Can't detect context — run on host (srv1) or workstation")


def cmd_snapshot(args: argparse.Namespace):
    """Create an Incus VM snapshot."""
    vm = args.vm
    name = f"rwa-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    print(f"Snapshoting {vm}/{name}...")

    result = run(["incus", "snapshot", vm, name])
    if result.returncode == 0:
        ok(f"Snapshot {name} created for {vm}")
        return

    # Try via SSH to host
    warn("Local incus failed, trying via SSH to srv1...")
    result = run(["ssh", "srv1", "incus", "snapshot", vm, name])
    if result.returncode == 0:
        ok(f"Snapshot {name} created for {vm} on srv1")
    else:
        die(f"Snapshot failed: {result.stderr[:200]}")


# ─── Build parser ────────────────────────────────────────────────────────────


def _add_host_flag(parser):
    """Add --host flag for remote execution."""
    parser.add_argument("--host", default=None,
                        help=f"Remote host (default: auto-detect — srv1)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rwa",
        description="RapidWebs AI Agent — backup, restore, infrastructure CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # backup
    bp = sub.add_parser("backup", help="Backup operations")
    bsub = bp.add_subparsers(dest="subcommand", required=True)
    br = bsub.add_parser("run", help="Run backup pipeline")
    _add_host_flag(br)
    br.add_argument("--vm", help="Specific VM to back up (default: all)")
    br.set_defaults(func=cmd_backup_run)

    bs = bsub.add_parser("status", help="Show backup status")
    _add_host_flag(bs)
    bs.set_defaults(func=cmd_backup_status)

    # restore
    rp = sub.add_parser("restore", help="Restore operations")
    rsub = rp.add_subparsers(dest="subcommand", required=True)

    rl = rsub.add_parser("list", help="List available snapshots")
    _add_host_flag(rl)
    rl.add_argument("vm", nargs="?", default=None, help="VM name (default: all)")
    rl.set_defaults(func=cmd_restore_list)

    rlat = rsub.add_parser("latest", help="Restore latest snapshot")
    _add_host_flag(rlat)
    rlat.add_argument("vm", help="VM name")
    rlat.add_argument("--target", help="Restore target path")
    rlat.set_defaults(func=cmd_restore_latest)

    rs = rsub.add_parser("snapshot", help="Restore specific snapshot by ID")
    _add_host_flag(rs)
    rs.add_argument("vm", help="VM name")
    rs.add_argument("snapshot_id", help="Snapshot ID to restore")
    rs.add_argument("--target", help="Restore target path")
    rs.set_defaults(func=cmd_restore_snapshot)

    # playbook
    pp = sub.add_parser("playbook", help="Ansible playbook operations")
    psub = pp.add_subparsers(dest="subcommand", required=True)

    pl = psub.add_parser("list", help="List available playbooks")
    pl.set_defaults(func=cmd_playbook_list)

    pr = psub.add_parser("run", help="Run a playbook")
    pr.add_argument("vm", help="VM name (infra, enterprise, dev, host)")
    pr.add_argument("--check", action="store_true", help="Dry-run mode")
    pr.set_defaults(func=cmd_playbook_run)

    # standalone
    sp = sub.add_parser("status", help="Overall system health")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("sync", help="Sync vdrive image")
    sp.add_argument("--target", help="Target for push/pull (default: auto-detect)")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("snapshot", help="Create Incus VM snapshot")
    sp.add_argument("vm", help="VM name (e.g., dev)")
    sp.set_defaults(func=cmd_snapshot)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
