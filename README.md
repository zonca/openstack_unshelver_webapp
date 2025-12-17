# OpenStack Unshelver Web App

Release `2025.09.22` is tied to the accompanying blog post: https://www.zonca.dev/posts/2025-09-22-openstack-unshelver-demo

FastHTML web application that authenticates users via GitHub OAuth (restricted to a
particular organisation) and provides buttons that unshelve and monitor configured
OpenStack instances.

## Features
- GitHub OAuth login with organisation membership enforcement
- Config-driven list of OpenStack instances to unshelve
- Unshelve workflow with live status updates every 10 seconds
- HTTP readiness probing and direct link to the instance web UI once online

## Configuration
Create a configuration file following [`config.example.yaml`](config.example.yaml) and
supply it as `config.yaml` or via the `UNSHELVER_CONFIG` environment variable.

Key sections:
- `app`: UI title, session secret, polling and HTTP probe timings
- `github`: OAuth client credentials, redirect URI, required organisation
- `openstack`: Authentication credentials passed to `openstacksdk`
- `buttons`: One entry per UI button with instance name and optional behaviour overrides

## Running the App
This project uses [uv](https://github.com/astral-sh/uv).

```bash
uv run python app.py
```

By default the app listens on `http://localhost:5001`. Adjust the `redirect_uri`
accordingly in GitHub OAuth settings and the configuration file.

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

The cosmosage version of this is available in the [`cosmosage_unshelver` branch](https://github.com/zonca/openstack_unshelver_webapp/tree/cosmosage_unshelver). Link: https://www.zonca.dev/posts/2025-12-16-cosmosage-unshelver
