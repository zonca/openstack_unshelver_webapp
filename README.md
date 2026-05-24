# OpenStack Unshelver Web App

A controller-based web application that manages the lifecycle of a GPU VM on Jetstream2.
A small always-on controller instance hosts the FastHTML web UI and Caddy reverse proxy,
while a large GPU instance is shelved by default and only woken on demand. Idle traffic
monitoring auto-shelves the GPU after a configurable timeout, keeping costs near zero.

Release `2025.09.22` is tied to the accompanying blog post: https://www.zonca.dev/posts/2025-09-22-openstack-unshelver-demo

The current iteration (branch `feature/controller-redesign`) refactors the FastHTML web
application into a controller that always stays online, exposes a public "wake GPU" button,
and keeps forwarding traffic to a Caddy reverse proxy that either serves the controller
UI or transparently proxies into the GPU VM once it is awake. A background watcher tails
Caddy's JSON access log to detect idle periods, automatically shelves the GPU, and records
every transition locally and in OpenStack Swift for durability.

## Features

- Public landing page that wakes the GPU VM and keeps polling its readiness
- `/control` surface gated by a shared token that exposes manual start/stop actions
- Idle detection via Caddy access logs with auto-shelve when no traffic hits the GPU
- Scheduling failure detection — if OpenStack cannot schedule the unshelve (e.g., GPU
  capacity shortage), the error is surfaced to the user with a helpful message
- Email notification on unshelve failure (via local Postfix/sendmail)
- JSONL event logging on disk plus mirroring to an OpenStack Swift container
- Dual-host routing so one hostname always shows the launcher while a second hostname
  proxies the GPU chat UI and falls back gracefully when the VM sleeps
- Automatic GPU VM startup via systemd (`llama-server.service`) and Cinder volume
  auto-mount via `/etc/fstab` — no manual intervention after unshelve

## Configuration

Create a configuration file following [`config.example.yaml`](config.example.yaml) and
supply it as `config.yaml` or via the `UNSHELVER_CONFIG` environment variable.

Key sections:

- `app`: UI title, session secret, probe timings, `control_token`, and the hidden manual
  shelve path.
- `openstack`: Authentication credentials passed to `openstacksdk`. These credentials are
  also used to upload audit events into Swift.
- `buttons`: Exactly one entry describing the GPU instance to control (health endpoint,
  launch path, network hints, and the optional `public_base_url` that tells the UI which
  controller hostname should be presented to end users).
- `activity_*`: Path to the Caddy JSON log file, idle thresholds, and upstream label used
  to identify which entries represent proxied GPU traffic.
- `local_event_log` + `swift_event_*`: paths and container details for durable logging.
- `notification_email`: Email address to notify on unshelve failures (uses local
  sendmail/Postfix on the controller VM).

## Running the App

This project uses [uv](https://github.com/astral-sh/uv).

```bash
uv run python app.py
```

By default the app listens on `http://localhost:5001`; point your local Caddy reverse
proxy at that port when running the controller locally.

## Testing

Run the unit tests with:

```bash
uv run pytest
```

## Deployment Architecture

```
Internet
  │
  ├─ cosmosage-unshelver.*.jetstream-cloud.org  → Caddy → FastHTML app (port 5001)
  │                                                 ├─ "/"         : public "Wake" button
  │                                                 ├─ "/control"  : admin dashboard (token-gated)
  │                                                 └─ idle monitor: auto-shelve after 120 min
  │
  └─ cosmosage.*.jetstream-cloud.org             → Caddy → GPU VM (port 8080)
                                                     └─ llama-server (AstroSage-70B)
```

### Controller VM

- Runs the FastHTML app (uvicorn on port 5001), Caddy (TLS + reverse proxy), and
  Postfix (for email notifications).
- The Caddy log must be JSON format so the idle monitor can parse upstream identifiers.

### GPU VM

- Runs `llama-server` as a systemd service (`llama-server.service`, enabled on boot).
- The Cinder volume containing model weights is auto-mounted via `/etc/fstab`.
- After an unshelve, the full stack comes up automatically — no manual SSH needed.
- **Important:** Only `g3.xl` (full A100 passthrough) works for inference. Fractional
  vGPU flavors (`g3.medium`, `g3.large`) have massive virtualization overhead that makes
  CUDA compute ~50× slower than CPU.

### Caddy Configuration

```caddy
{
    admin off
}

chat.example.org {
    log {
        output file /var/log/caddy/gpu-access.log {
            roll_keep 7
        }
        format json
    }

    handle_path /control* {
        reverse_proxy 127.0.0.1:5001
    }

    handle {
        reverse_proxy {
            upstream gpu-backend 203.0.113.42:443
            header_down X-Served-By gpu
            fail_duration 0s
        }

        handle_response 502 503 504 {
            reverse_proxy 127.0.0.1:5001
        }
    }

    handle {
        reverse_proxy 127.0.0.1:5001
    }
}
```

The `gpu-backend` label must match `caddy_upstream_label` in `config.yaml`.

## Email Notifications

When an unshelve fails (typically due to GPU capacity shortages), the controller sends
an email to the configured `notification_email` address via local Postfix/sendmail.

Requirements on the controller VM:
- Postfix must be installed and running (`sudo systemctl enable --now postfix`).
- On the default Jetstream2 Ubuntu 24 image, Postfix is masked — run
  `sudo systemctl unmask postfix && sudo systemctl enable --now postfix`.

## Notes

- The web UI keeps state in-memory; if you run multiple processes you should add a
  shared backing store for task status.
- TLS certificate verification for instance readiness checks can be disabled per button
  using `verify_tls: false` when required for self-signed certificates.
- The OpenStack credentials can be provided either as username/password/project or
  application credentials (set `application_credential_id` and
  `application_credential_secret`).
- Use `uv run python scripts/ensure_dns_record.py <hostname> <ip>` to create/update the
  Designate A records that point public hostnames to the controller VM.
- The idle monitor only watches Caddy proxied traffic — direct SSH sessions on the GPU VM
  do not reset the idle timer.
