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
            github:
              client_id: cid
              client_secret: secret
              redirect_uri: http://localhost/callback
              organization: acme
            openstack:
              auth_url: https://example.com
              username: user
              password: pass
              project_name: proj
            buttons:
              - id: button-one
                label: App One
                instance_name: instance-one
            """
        ).strip()
    )

    settings = load_settings(str(config))

    assert settings.app.title == "Test"
    assert settings.github.organization == "acme"
    assert settings.buttons[0].id == "button-one"


def test_load_settings_raises_on_invalid(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        textwrap.dedent(
            """
            app:
              title: Test
              secret_key: short
            github:
              client_id: cid
              client_secret: secret
              redirect_uri: http://localhost/callback
              organization: acme
            openstack:
              auth_url: https://example.com
              username: user
              password: pass
              project_name: proj
            buttons: []
            """
        ).strip()
    )

    with pytest.raises(ConfigurationError):
        load_settings(str(config))
