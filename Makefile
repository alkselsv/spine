UV ?= uv
PORT ?= 8080
PYTEST_ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help install test test-fast test-release compile diff-check check verify api

help: ## Show available commands.
	@printf '%s\n' \
		'make install  Install development dependencies' \
		'make test     Run the mandatory full test suite, including PostgreSQL' \
		'make test-fast Run developer feedback tests without PostgreSQL acceptance' \
		'make test-release Run tests, compilation, and diff validation' \
		'make compile  Compile Python sources and tests' \
		'make check    Run tests and compilation checks' \
		'make verify   Install dependencies and run all checks' \
		'make api      Start the development API (PORT=8080)'

install: ## Install development dependencies.
	$(UV) sync --extra dev

test: ## Run the test suite; pass extra options with PYTEST_ARGS.
	$(UV) run pytest -q $(PYTEST_ARGS)

test-fast: ## Run narrower feedback only; this is not a release gate.
	@printf '%s\n' 'DEVELOPER FEEDBACK ONLY: PostgreSQL acceptance has not run.'
	$(UV) run pytest -q -m 'not postgresql' $(PYTEST_ARGS)

compile: ## Compile Python sources and tests.
	$(UV) run python -m compileall -q src tests

diff-check: ## Reject whitespace and conflict-marker errors in the working diff.
	git diff --check

test-release: test compile diff-check ## Run the complete release gate without sync.

check: test-release ## Run all checks without installing dependencies.

verify: install check ## Run the complete repository verification gate.

api: ## Start the development API; override PORT if needed.
	$(UV) run uvicorn spine.api.main:app --reload --port $(PORT)
