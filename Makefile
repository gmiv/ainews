# ai_news_feed — venv workflow (the launcher owns the venv; this just delegates).
# The venv lives off the Windows drive on WSL for speed; ask the launcher where.
PY := $(shell python3 ai_news_feed.py --venv-python)

.PHONY: help run setup test lint clean

help:
	@echo "make setup   build the isolated venv (no launch)"
	@echo "make run     launch the TUI (bootstraps the venv if needed)"
	@echo "make test    run the test suite in the venv"
	@echo "make lint    ruff check in the venv"
	@echo "make clean   remove the venv + build artifacts + caches"

run:
	./run

setup:
	./run --setup

test: setup
	$(PY) -m pip install -q pytest
	$(PY) -m pytest -q

lint: setup
	$(PY) -m pip install -q ruff
	$(PY) -m ruff check . || true

clean:
	python3 ai_news_feed.py --clean
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -not -path './.git/*' -exec rm -rf {} +
