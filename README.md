# RW-AIAI — RapidWebs Automated Infrastructure AI Agent

**Infrastructure as Code + LangGraph-backed backup pipeline + self-healing supervisor.**

A single project that declaratively defines every VM and container in the RapidWebs fleet, snapshots them on a schedule, audits the results, and escalates failures — so a rogue agent session or a disk failure costs us a `git pull && ansible-playbook` instead of a rebuild from scratch.

## Architecture

```
Hetzner CX32 (srv1.rapidwebs.org)
├── Incus host (bare metal)
│   ├── infra VM      — PG16, Caddy, agentgateway, Hermes gateway
│   ├── enterprise VM — application workloads
│   └── dev VM        — development services, Hermes worker
├── /mnt/vdrive/      — dedicated backup volume (on host)
└── rw_aiai/          — this repo
    ├── ansible/      — declarative state per VM
    ├── backup/       — LangGraph pipeline
    ├── supervisor/   — audit + auto-fix agent
    └── scripts/      — shell helpers
```

## What Each Piece Does

| Layer | What | Failure Mode It Prevents |
|---|---|---|
| **Ansible playbooks** | Declare every package, config file, systemd service, and user per VM | "How do I rebuild that from scratch?" |
| **LangGraph backup** | Incremental encrypted backups for infra/enterprise; snapshots for dev | "The VM just got wiped by a bad command" |
| **Supervisor agent** | Watches backup audit logs, retries failures, escalates if stuck | "The backup silently failed for 3 weeks" |
| **IaC drift reflection** | Checks actual VM state vs Ansible, suggests/pushes updates | "The playbook is 6 months out of date" |

## Quick Start

```bash
# Install deps
pip install -e .

# Run Ansible against a VM
cd ansible && ansible-playbook -i inventory.yml infra/playbook.yml

# Run backup pipeline
python -m rw_aiai.backup.pipeline --config backup/config.yml

# Run supervisor agent
python -m rw_aiai.supervisor.agent --check
```
