import textwrap

import pytest

from openstack_unshelver_webapp.config import ConfigurationError, Settings, load_settings


def test_load_settings_success(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        textwrap.dedent(
            """
            app:
              title: Test
              secret_key: 1234567890abcdef
              poll_interval_seconds: 10
              http_probe_timeout: 5
              http_probe_attempts: 3
              control_token: 0123456789abcdef
              manual_shelve_path: /admin-shelve
            openstack:
              auth_url: https://example.com
              username: user
              password: pass
              project_name: proj
            buttons:
              - id: button-one
                label: App One
                instance_name: instance-one
            activity_log_path: /tmp/caddy.log
            idle_timeout_minutes: 60
            idle_poll_interval_seconds: 30
            caddy_upstream_label: gpu
            local_event_log: logs/events.jsonl
            swift_event_container: null
            swift_event_prefix: events
            """
        ).strip()
    )

    settings = load_settings(str(config))

    assert settings.app.title == "Test"
    assert settings.buttons[0].id == "button-one"


def test_load_settings_raises_on_invalid(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        textwrap.dedent(
            """
            app:
              title: Test
              secret_key: short
            openstack:
              auth_url: https://example.com
              username: user
              password: pass
              project_name: proj
            buttons: []
            activity_log_path: /tmp/caddy.log
            idle_timeout_minutes: 60
            idle_poll_interval_seconds: 30
            caddy_upstream_label: gpu
            local_event_log: logs/events.jsonl
            swift_event_container: null
            swift_event_prefix: events
            """
        ).strip()
    )

    with pytest.raises(ConfigurationError):
        load_settings(str(config))
