# OpenStack Unshelver Web App

Release `2025.09.22` is tied to the accompanying blog post: https://www.zonca.dev/posts/2025-09-22-openstack-unshelver-demo

The next iteration refactors the FastHTML web application into a controller that always
stays online, exposes a public “wake GPU” button, and keeps forwarding traffic to a Caddy
reverse proxy that either serves the controller UI or transparently proxies into the GPU
VM once it is awake. A background watcher tails Caddy’s JSON access log to detect idle
periods, automatically shelves the GPU, and records every transition locally and in
OpenStack Swift for durability.

## Features
- Public landing page that wakes the GPU VM and keeps polling its readiness
- `/control` surface gated by a shared token that exposes manual start/stop actions
- Idle detection via Caddy access logs with auto-shelve when no traffic hits the GPU
- JSONL event logging on disk plus mirroring to an OpenStack Swift container
- Dual-host routing so one hostname always shows the launcher while a second hostname proxies the GPU chat UI and falls back gracefully when the VM sleeps

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
  controller hostname should be presented to end users.
- `activity_*`: Path to the Caddy JSON log file, idle thresholds, and upstream label used
  to identify which entries represent proxied GPU traffic.
- `local_event_log` + `swift_event_*`: paths and container details for durable logging.

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

## Next Version Architecture Plan

We are planning a controller-centric deployment where a tiny OpenStack instance keeps the
web UI, orchestration logic, and Caddy reverse proxy online at all times while the large
GPU instance is shelved by default.

- **Process layout**: the controller VM runs the FastHTML app (without GitHub auth and
  with a public landing page), Caddy serving TLS + reverse proxy duties, and a background
  watcher that streams Caddy access logs to track activity.
- **Dual hostnames**: `cosmosage-unshelver…` always lands on the launcher UI while
  `cosmosage…` proxies to the GPU VM. When the GPU is offline, Caddy detects the failure
  and redirects the chat hostname back to the launcher.
- **Wake + proxy**: when a visitor presses the public button, the FastHTML app unshelves
  the GPU VM, polls its `/health`, and posts a friendly message telling people to open
  the chat hostname once it is ready.
- **Idle detection**: the controller watches Caddy’s proxied requests; if no traffic hits
  the GPU backend for the configured timeout, a background task triggers the shelve
  workflow. This exists alongside a manual shelve-now endpoint exposed only at an obscure
  admin URL.
- **Control surface**: `/control` shows current OpenStack status, last activity timestamp,
  upcoming idle shutdown, and embeds manual start/stop buttons plus recent log entries.
- **Event logging**: every state change is appended locally (JSONL with rotation) and
  mirrored to OpenStack Swift using the same credentials, creating an external durable
  audit trail.
- **Reconciliation**: on boot the controller reads the GPU instance status and ensures the
  Caddy fallback flag reflects reality. Periodic checks keep Caddy, OpenStack, and the UI
  in sync even if an action fails midway.

This plan keeps the GPU instance costs near zero while presenting a seamless launcher
experience and a persistent control plane for administrators.

### Controller Deployment Notes

1. **FastHTML app**: run via `uv run python app.py` on the controller. Expose port 5001
   locally and keep the `.env` or `config.yaml` with the control token and OpenStack
   credentials. Ensure the process has permission to read the configured Caddy log path.
2. **Caddy**: configure TLS on the controller VM and use a static config similar to:

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

   The `gpu-backend` label must match `caddy_upstream_label` in `config.yaml`, and the log
   format must be JSON so the controller can parse upstream identifiers.
3. **Idle watcher**: the controller tails `/var/log/caddy/gpu-access.log`, so rotate the
   file gently (Caddy snippet above keeps 7 days) and ensure the controller process can
   read new entries.
4. **Swift logging**: create the target container up front or let the controller create it
   automatically. Each event writes its own object under the configured prefix using the
   same OpenStack credentials already needed for compute operations.
