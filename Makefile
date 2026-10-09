UV ?= uv
PORT ?= 8080
PYTEST_ARGS ?=

.DEFAULT_GOAL := help

.PHONY: help install test compile check verify api

help: ## Show available commands.
	@printf '%s\n' \
		'make install  Install development dependencies' \
		'make test     Run the test suite' \
		'make compile  Compile Python sources and tests' \
		'make check    Run tests and compilation checks' \
		'make verify   Install dependencies and run all checks' \
		'make api      Start the development API (PORT=8080)'

install: ## Install development dependencies.
	$(UV) sync --extra dev

test: ## Run the test suite; pass extra options with PYTEST_ARGS.
	$(UV) run pytest -q $(PYTEST_ARGS)

compile: ## Compile Python sources and tests.
	$(UV) run python -m compileall -q src tests

check: test compile ## Run all checks without installing dependencies.

verify: install check ## Run the complete repository verification gate.

api: ## Start the development API; override PORT if needed.
	$(UV) run uvicorn spine.api.main:app --reload --port $(PORT)
