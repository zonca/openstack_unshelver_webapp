.PHONY: run test

run:
	uv run python app.py

test:
	uv run pytest
