.PHONY: run test test-openstack-live

run:
	uv run python app.py

test:
	uv run pytest

test-openstack-live:
	uv run pytest tests/test_openstack_live_credentials.py
