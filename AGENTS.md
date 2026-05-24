# AGENTS.md — OpenStack Unshelver Web App

## Project Overview

A controller-based web app that manages the lifecycle of a GPU VM on Jetstream2.
A small always-on controller instance hosts the FastHTML web UI and Caddy reverse proxy,
while a large GPU instance (running llama.cpp for the Cosmosage chatbot) is shelved by
default and only woken on demand. Idle traffic monitoring auto-shelves the GPU after a
configurable timeout, keeping costs near zero.

**Repository:** `https://github.com/zonca/openstack_unshelver_webapp`
**Branch:** `feature/controller-redesign` (active development)
**Issue tracker:** `https://github.com/tijmen/cosmosage/issues/4` (deployment tracking)

---

## Architecture

```
Internet
  │
  ├─ cosmosage-unshelver.*.jetstream-cloud.org  → Caddy → FastHTML app (port 5001)
  │                                                 ├─ "/"         : public "Wake" button
  │                                                 ├─ "/control"  : admin dashboard (token-gated)
  │                                                 └─ idle monitor: auto-shelve after 120 min
  │
  └─ cosmosage.*.jetstream-cloud.org             → Caddy → GPU VM (149.165.155.205:8080)
                                                     └─ llama-server (AstroSage-70B)
```

Two hostnames, one controller VM:

1. **Launcher hostname** (`cosmosage-unshelver…`) — always serves the FastHTML app.
2. **Chat hostname** (`cosmosage…`) — Caddy reverse-proxies to the GPU VM when it is
   ACTIVE; falls back to the launcher UI when the VM is shelved (502/503/504 handler).

---

## Deployment: Jetstream2 IU

### Controller VM

| Field | Value |
|-------|-------|
| Hostname | `cosmosage-unshelver` |
| IP | `149.165.172.14` |
| SSH | `ssh -i ~/.ssh/juicessh_id_rsa exouser@149.165.172.14` |
| Region | IU |
| Services | FastHTML app (uvicorn on :5001), Caddy (TLS + reverse proxy), Postfix (email) |
| App dir | `~/openstack_unshelver_webapp/` |
| Config | `~/openstack_unshelver_webapp/config.yaml` |
| App log | `/tmp/unshelver.log` (uvicorn stdout) |
| Event log | `/var/log/unshelver/events.jsonl` + Swift container `cosmosage-unshelver-events` |
| Caddy log | `/var/log/caddy/gpu-access.log` (JSON format, for idle detection) |

### GPU VM

| Field | Value |
|-------|-------|
| Instance name | `cosmosage_70b_zonca` |
| Flavor | `g3.xl` (full A100-SXM4-40GB, passthrough — NOT vGPU) |
| Floating IP | `149.165.155.205` |
| SSH | `ssh -i ~/.ssh/juicessh_id_rsa ubuntu@149.165.155.205` |
| Cinder volume | `llmstorage` (attached as `/dev/sdb`, mounted at `/mnt/llmstorage`) |
| Volume UUID | `826c86d8-aabf-4748-8f9f-fd9b4e54b7be` |
| fstab entry | `UUID=826c86d8-… /mnt/llmstorage ext4 defaults,nofail 0 2` |
| Model file | `/mnt/llmstorage/AstroSage-70B-20251009.i1-IQ3_XXS.gguf` (26 GB) |
| Service | `llama-server.service` (systemd, enabled) |
| Service command | `llama-server -m /mnt/llmstorage/AstroSage-70B-20251009.i1-IQ3_XXS.gguf --port 8080 --host 0.0.0.0 -ngl 99 -c 4096 --parallel 2 -t 16 --timeout 600 --cache-ram 0` |
| Health endpoint | `http://149.165.155.205:8080/health` |
| Chat endpoint | `https://cosmosage.phy240259.projects.jetstream-cloud.org/` |
| Performance | ~25 tok/s generation, ~177 tok/s prompt processing |

---

## Key Operational Facts

### GPU Flavor Warning
- **Only `g3.xl` works for inference.** The fractional vGPU flavors (`g3.medium` = A100X_10C, `g3.large` = A100X_20C) have massive virtualization overhead that makes CUDA compute ~50× slower than CPU.
- `g3.xl` capacity on Jetstream2 IU is extremely tight. Unshelve often fails with "No valid host was found." The unshelver detects this scheduling failure and surfaces it to the user.
- **AstroSage-70B IQ3_XXS** (26 GB) fits entirely in the 40 GB A100 VRAM. The Q3_K_L variant (35 GB) does NOT fit — no room for KV cache.

### Auto-Start on Unshelve
The GPU VM has a systemd service (`llama-server.service`) that auto-starts on boot, and the `llmstorage` volume is in `/etc/fstab` so it auto-mounts. After an unshelve, the full stack comes up automatically — no manual SSH needed.

### Email Notifications
Postfix is running on the controller VM. When an unshelve fails, `email_notifier.py` sends an alert to the configured `notification_email` (currently `zonca@sdsc.edu`) via local sendmail. Postfix relays directly to `inbound.ucsd.edu`.

### Idle Auto-Shelve
The `CaddyActivityMonitor` watches `/var/log/caddy/gpu-access.log`. If no proxied request hits the GPU upstream for `idle_timeout_minutes` (120 min), it triggers `start_shelve()` automatically. The idle timeout counter resets on any proxied traffic.

### Model Files on llmstorage Volume
| File | Size | Notes |
|------|------|-------|
| `AstroSage-70B-20251009.i1-IQ3_XXS.gguf` | 26 GB | **Active model** — fits in 40 GB A100 |
| `Meta-Llama-3.1-70B-Instruct-Q3_K_L.gguf` | 35 GB | Too large for 40 GB A100 (no room for KV cache) |
| `cosmosage-v3.i1-Q4_K_M.gguf` | 4.6 GB | 7B fallback for g3.medium (CPU-only) |

---

## Codebase Layout

```
app.py                          — FastHTML web app (routes, UI, startup hooks)
config.yaml                     — Live config (secrets, OpenStack creds, button defs)
config.example.yaml             — Template config (no secrets)
openstack_unshelver_webapp/
  __init__.py                   — Package exports
  config.py                     — Pydantic settings model (AppSettings, OpenStackSettings, ButtonSettings, Settings)
  openstack_client.py           — OpenStack SDK wrapper (find_server, unshelve, shelve, build_endpoint)
  unshelve_manager.py           — Core orchestration: unshelve/shelve workflows, status tracking, HTTP probing
  activity.py                   — CaddyActivityMonitor: tails Caddy JSON log, triggers idle callback
  event_logger.py               — EventLogger: JSONL local log + Swift mirror
  email_notifier.py             — send_failure_notification(): sends email via local sendmail on unshelve failure
  github.py                     — GitHub OAuth flow (unused in controller mode)
scripts/
  ensure_dns_record.py          — Create/update Designate A records for public hostnames
tests/
  conftest.py
  test_config.py
  test_openstack_live_credentials.py
  test_unshelve_manager.py
```

### Key Design Patterns

- **In-memory state:** Button statuses are held in `InstanceActionManager._statuses` (dict). No database. State resets on app restart.
- **Async orchestration:** Unshelve/shelve workflows run as `asyncio.Task`s. The web UI polls `/status/{button_id}` via HTMX every `poll_interval_seconds`.
- **Transient error retry:** All OpenStack API calls have retry logic (`_find_server_with_retry`, `_unshelve_with_retry`, `_poll_server_with_retry`) for DNS/connection drops.
- **Scheduling failure detection:** If the instance stays `SHELVED_OFFLOADED` with no `task_state` for 3+ polls after an unshelve request, the manager checks `os-instance-actions` API. If the unshelve action has `message: Error`, it raises `RuntimeError` with a user-friendly message about GPU availability.
- **HTTP readiness probing:** After the instance becomes ACTIVE, the manager probes `healthcheck_path` up to `http_probe_attempts` times (18 by default) before declaring the service ready or not.

---

## Common Operations

### Restart the unshelver app
```bash
ssh -i ~/.ssh/juicessh_id_rsa exouser@149.165.172.14
pkill -f "uvicorn app:app"
cd ~/openstack_unshelver_webapp
nohup .venv/bin/python .venv/bin/uvicorn app:app \
    --host 0.0.0.0 --port 5001 --proxy-headers \
    > /tmp/unshelver.log 2>&1 &
```

### Force shelve the GPU VM
```bash
# Via control panel (browser):
# https://cosmosage-unshelver.*.jetstream-cloud.org/control?token=<CONTROL_TOKEN>

# Via OpenStack SDK:
cd ~/openstack_unshelver_webapp
.venv/bin/python -c "
from openstack import connection
conn = connection.Connection(auth_url='...', region_name='IU', ...)
srv = conn.compute.find_server('cosmosage_70b_zonca')
conn.compute.shelve_server(srv)
"
```

### Manually unshelve the GPU VM
```bash
# Same as above but:
conn.compute.unshelve_server(srv)
```

### Check GPU VM status
```bash
ssh -i ~/.ssh/juicessh_id_rsa ubuntu@149.165.155.205
sudo systemctl status llama-server
curl -s http://127.0.0.1:8080/health
nvidia-smi
```

### Test email notification
```bash
cd ~/openstack_unshelver_webapp
.venv/bin/python -c "
from openstack_unshelver_webapp.email_notifier import send_failure_notification
send_failure_notification(
    recipient='zonca@sdsc.edu',
    instance_name='cosmosage_70b_zonca',
    error_message='TEST: manual notification test',
)
"
```

---

## Known Issues & Pitfalls

1. **g3.xl capacity is scarce.** Unshelve frequently fails. The email notifier alerts on failure, but users must retry later.
2. **vGPU flavors are broken for llama.cpp.** `g3.medium`/`g3.large` use NVIDIA GRID vGPU with fractional compute cores. CUDA `matmul` works but llama.cpp inference is 50× slower than CPU. Only `g3.xl` (full A100 passthrough) is viable.
3. **Postfix must be running for email.** On controller VM boot, ensure `sudo systemctl start postfix` (or enable it). Postfix was masked by default on the Jetstream2 Ubuntu 24 image.
4. **config.yaml is in .gitignore** — it contains OpenStack application credential secrets. Never commit it.
5. **The `-ngl 99` flag hangs on vGPU.** Use an explicit layer count (e.g., `-ngl 33`) or `-ngl 0` for CPU-only. The "fitting params to device memory" auto-detection algorithm loops indefinitely on fractional vGPU profiles.
6. **Instance can be auto-shelved while you are SSH'd in.** The idle monitor only watches Caddy proxied traffic. If you are working on the GPU VM directly without chatting through the web UI, the 120-minute idle timer will still fire.
