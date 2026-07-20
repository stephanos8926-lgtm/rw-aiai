# Architecture

**Two LangGraph pipelines** orchestrate everything:

## Backup Pipeline (`src/rw_aiai/backup/pipeline.py`)

```
load_config → for_each_vm (pre_hook → backup → post_hook → verify)
 → prune → write_audit → check_audit
                              ├── ok → END
                              └── error → escalate → END
```

- **incremental** VMs (infra, enterprise): restic over SSH, compressed + encrypted
- **snapshot** VMs (dev): incus snapshot with auto-naming
- **retention**: daily × 7, weekly × 4, monthly × 3 (configurable)
- **audit**: JSON-per-run stored under `backup_root/audit/`

## Supervisor Agent (`src/rw_aiai/supervisor/agent.py`)

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

## IaC Drift Detector (`src/rw_aiai/iac/`)

Checks each host in `ansible/inventory.yml` for reachability and basic state.
Extended in future to diff actual package/version against playbook declarations.

# Deployment

1. Install dependencies: `pip install -e .`
2. Configure `backup/config.yml` paths and retention
3. Set up `ansible/inventory.yml` with correct Tailscale IPs
4. Initial provision: `cd ansible && ansible-playbook -i inventory.yml infra/playbook.yml`
5. Schedule via cron/systemd timer:

```cron
# Nightly backup at 2am
0 2 * * * cd /path/to/rw_aiai && python -m rw_aiai.backup.pipeline --config backup/config.yml

# Supervisor check at 2:30am
30 2 * * * cd /path/to/rw_aiai && python -m rw_aiai.supervisor.agent --audit-dir /mnt/vdrive/backups/audit
```
