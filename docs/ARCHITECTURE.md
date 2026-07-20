# Architecture

## Overview

```
                    ┌─────────────────────────────────────┐
                    │           Host (srv1)                │
                    │                                     │
                    │  /vdrive/backup.img (btrfs sparse)   │
                    │  mounted at /mnt/vdrive              │
                    │     ├── backups/infra/  (restic)     │
                    │     ├── backups/enterprise/ (restic) │
                    │     ├── backups/dev/  (incus snaps)  │
                    │     └── audit/                      │
                    │                                     │
                    │  systemd: vdrive-manager.service     │
                    │           vdrive-maintenance.timer   │
                    │           vdrive-sync.timer          │
                    └──────────┬──────────────────────────┘
                               │ rsync (if enabled)
                               ▼
                    ┌─────────────────────────────────────┐
                    │     Workstation (rw-workstation-01)  │
                    │                                     │
                    │  /mnt/remote_backups/                │
                    │     ├── slot_1/backup.img (latest)   │
                    │     └── slot_2/backup.img (previous) │
                    │                                     │
                    │  Rotation: new → slot1,             │
                    │            old slot1 → slot2,       │
                    │            old slot2 → /dev/null     │
                    └─────────────────────────────────────┘
```

## Vdrive Layer (Host-side)

The **vdrive** is a sparse btrfs disk image. It only uses the physical blocks actually written — a 20G virtual image starts at ~1M real and grows as data fills it. The `discard` mount option and periodic `fstrim` return freed blocks to the host filesystem.

### Service: `vdrive-manager.service`

| Action | Trigger | What it does |
|---|---|---|
| `mount` | Boot | Reads `/etc/vdrive/config.yml`, creates image if missing, formats btrfs, mounts |
| `umount` | Shutdown | Safely unmounts all vdrives |
| `grow` | Daily timer | If usage > 80%, expands sparse image by 20% and resizes btrfs |
| `trim` | Daily timer | `fstrim` — returns freed blocks to host |
| `sync` | Timer (opt-in) | Rsyncs image to workstation (if `remote_backup.enabled: true`) |
| `status` | Manual | Shows mount health, size, actual disk usage |

### Files

| Path | Purpose |
|---|---|
| `/usr/local/bin/vdrive-manager` | The service script |
| `/etc/vdrive/config.yml` | YAML configuration |
| `/etc/systemd/system/vdrive-manager.service` | Boot mount unit |
| `/etc/systemd/system/vdrive-maintenance.service` | Daily grow+trim |
| `/etc/systemd/system/vdrive-maintenance.timer` | Daily timer (randomized) |
| `/etc/systemd/system/vdrive-sync.service` | Remote sync unit |
| `/etc/systemd/system/vdrive-sync.timer` | Sync timer (opt-in) |

### Sparse + Thin Allocation

The vdrive image is created with `dd if=/dev/zero of=... bs=1M count=1 seek=N` — a **sparse file** that reports N MB virtual but uses only the blocks actually written. Combined with:
- `mount -o discard` — filesystem tells the underlying file when blocks are freed
- `fstrim` (daily) — scavenges all freed blocks back to the host
- btrfs `compress=zstd` — compresses data inline, reducing physical usage

Result: the vdrive grows and shrinks with actual data, not its virtual capacity.

## Backup Pipeline (`src/rw_aiai/backup/pipeline.py`)

**LangGraph state machine** run on the host:

```
load_config → for_each_vm (pre_hook → backup → post_hook → verify)
 → prune → write_audit → check_audit
                              ├── ok → END
                              └── error → escalate → END
```

| VM Type | Method | Tool | Encryption |
|---|---|---|---|
| infra | Incremental (file-level) | restic over SSH | Built-in restic (AES-256) |
| enterprise | Incremental (file-level) | restic over SSH | Built-in restic (AES-256) |
| dev | Snapshot (VM-level) | incus snapshot | Block-level (btrfs on vdrive) |

Retention: daily × 7, weekly × 4, monthly × 3 (configurable in `backup/config.yml`).

## Supervisor Agent (`src/rw_aiai/supervisor/agent.py`)

**LangGraph agent** that watches backup audit logs and auto-fixes known patterns:

```
scan_audits → parse_latest → detect_issues → attempt_fix → assess
                                                             ├── all_fixed → report → END
                                                             ├── needs_escalation → escalate → report → END
                                                             └── retry_fix → attempt_fix (loop)
```

Known auto-fix patterns:
- restic repo missing → `restic init`
- SSH connection refused → wait + retry
- Backup volume full → aggressive prune
- incus not found → `apt install incus`

If no auto-fix succeeds → escalates to sysop via Telegram.

## Remote Sync (Workstation-side)

`scripts/vdrive-sync.sh` runs on **rw-workstation-01**:

1. Rsyncs `/vdrive/backup.img` from srv1 to local temp file
2. Compares checksum with `slot_1/backup.img`
3. Identical → discard temp, no-op
4. Different → rotate: `slot_1 → slot_2`, temp → `slot_1`
5. `slot_2` is always overwritten (old → /dev/null)

### Workstation systemd units

| Unit | Purpose |
|---|---|
| `vdrive-sync-local.service` | Trigger sync+rotation |
| `vdrive-sync-local.timer` | Daily at 4:30am (after host backup) |

## Ansible Playbooks

| Playbook | Target | Sets up |
|---|---|---|
| `host/playbook.yml` | srv1 (bare metal) | vdrive-manager, btrfs, systemd units, backup dirs |
| `infra/playbook.yml` | infra VM | PG16, Caddy, agentgateway, Hermes gateway |
| `enterprise/playbook.yml` | enterprise VM | Podman app scaffold |
| `dev/playbook.yml` | dev VM | Hermes worker, ast-tools, InferenceEngine |

Run order: `host → infra → enterprise → dev`

## Deployment

```bash
# 0. Install dependencies
pip install -e .

# 1. Configure
#    Edit backup/config.yml (retention, VM list)
#    Edit etc/vdrive-config.yml (image size, remote toggle)

# 2. Provision the host (srv1)
#    First time:  sudo ./scripts/vdrive-host-setup.sh
#    Thereafter:  cd ansible && ansible-playbook -i inventory.yml host/playbook.yml

# 3. Provision VMs
cd ansible && ansible-playbook -i inventory.yml infra/playbook.yml

# 4. Schedule backups (systemd timers already handle this)
systemctl status vdrive-maintenance.timer
systemctl status vdrive-sync.timer  # only if remote_backup enabled

# 5. Verify
sudo vdrive-manager status
python -m rw_aiai.backup.pipeline --config backup/config.yml
