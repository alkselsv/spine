UV ?= uv
PYTHON ?= python3
PORT ?= 8080
PYTEST_ARGS ?=
COMPOSE ?= docker compose
COMPOSE_WORKTREE = $(PYTHON) scripts/compose_worktree.py
COMPOSE_PROJECT_ARG = $(if $(strip $(COMPOSE_PROJECT)),--project "$(COMPOSE_PROJECT)")
COMPOSE_ENV_FILES = --env-file .env --env-file .env.qualification
COMPOSE_CONTEXT = $(COMPOSE_WORKTREE) context $(COMPOSE_PROJECT_ARG) $(COMPOSE_ENV_FILES)
COMPOSE_PREFLIGHT = $(COMPOSE_WORKTREE) preflight --compose "$(COMPOSE)" $(COMPOSE_PROJECT_ARG) $(COMPOSE_ENV_FILES)
COMPOSE_COMMAND = $(COMPOSE_WORKTREE) run --compose "$(COMPOSE)" $(COMPOSE_PROJECT_ARG) $(COMPOSE_ENV_FILES) --
LOG_TAIL ?= 200
LOG_SERVICES ?= api qa-worker outbox-dispatcher web
QUALIFICATION_TTL ?= 4h
PROVIDER_BUDGET ?=
PROVIDER_CURRENCY ?= USD
FORCE ?= 0
# Resolve macOS `/tmp` to `/private/tmp`: filesystem security tests reject
# symlinked path components such as `/var` and `/tmp` by design.
TEST_TMP_ROOT ?= $(realpath /tmp)
TEST_TMPDIR ?= $(TEST_TMP_ROOT)/spine-pytest-tmp
TEST_UV_CACHE_DIR ?= $(TEST_TMP_ROOT)/spine-uv-cache

.DEFAULT_GOAL := help

.PHONY: help install test test-fast test-release compile diff-check check verify api qualification-env worktree-init compose-env compose-context compose-preflight qualification-provision start start-fixtures status logs stop destroy

help: ## Show available commands.
	@printf '%s\n' \
		'make install  Install development dependencies' \
		'make test     Run the mandatory full test suite, including PostgreSQL' \
		'make test-fast Run developer feedback tests without PostgreSQL acceptance' \
		'make test-release Run tests, compilation, and diff validation' \
		'make compile  Compile Python sources and tests' \
		'make check    Run tests and compilation checks' \
		'make verify   Install dependencies and run all checks' \
		'make worktree-init Create missing private runtime and qualification env files' \
		'make qualification-env Generate a bounded local .env.qualification' \
		'make compose-context Show the automatic worktree Compose project and ports' \
		'make compose-preflight Check the selected ports without changing Docker state' \
		'make start    Provision qualification authority and start Compose' \
		'make start-fixtures Start the isolated fixture UI on port 4176' \
		'make status   Show all Compose services' \
		'make logs     Show recent application service logs' \
		'make stop     Stop Compose and preserve its volumes' \
		'make destroy  Delete Compose containers and volumes' \
		'make api      Start the development API (PORT=8080)'

install: ## Install development dependencies.
	$(UV) sync --extra dev

test: ## Run the test suite; pass extra options with PYTEST_ARGS.
	@mkdir -p "$(TEST_TMPDIR)" "$(TEST_UV_CACHE_DIR)"
	TMPDIR="$(TEST_TMPDIR)" UV_CACHE_DIR="$(TEST_UV_CACHE_DIR)" $(UV) run pytest -q $(PYTEST_ARGS)

test-fast: ## Run narrower feedback only; this is not a release gate.
	@printf '%s\n' 'DEVELOPER FEEDBACK ONLY: PostgreSQL acceptance has not run.'
	@mkdir -p "$(TEST_TMPDIR)" "$(TEST_UV_CACHE_DIR)"
	TMPDIR="$(TEST_TMPDIR)" UV_CACHE_DIR="$(TEST_UV_CACHE_DIR)" $(UV) run pytest -q -m 'not postgresql' $(PYTEST_ARGS)

compile: ## Compile Python sources and tests.
	$(UV) run python -m compileall -q src tests scripts

diff-check: ## Reject whitespace and conflict-marker errors in the working diff.
	git diff --check

test-release: test compile diff-check ## Run the complete release gate without sync.

check: test-release ## Run all checks without installing dependencies.

verify: install check ## Run the complete repository verification gate.

qualification-env: ## Generate .env.qualification; use FORCE=1 to replace it.
	@mkdir -p "$(TEST_UV_CACHE_DIR)"
	UV_CACHE_DIR="$(TEST_UV_CACHE_DIR)" $(UV) run spine-qualification-env \
		--ttl "$(QUALIFICATION_TTL)" \
		--provider-currency "$(PROVIDER_CURRENCY)" \
		$(if $(strip $(PROVIDER_BUDGET)),--provider-budget "$(PROVIDER_BUDGET)") \
		$(if $(filter 1 true yes,$(FORCE)),--force)

worktree-init: ## Create missing private env files without replacing existing files.
	@$(COMPOSE_WORKTREE) init --template .env.example --runtime .env
	@if test -f .env.qualification; then \
		printf '%s\n' 'qualification environment kept: .env.qualification'; \
	else \
		$(MAKE) qualification-env; \
	fi

compose-env: ## Fail before Docker when required local environment files are absent.
	@test -f .env || { printf '%s\n' 'Не найден .env; выполните make worktree-init.' >&2; exit 2; }
	@test -f .env.qualification || { printf '%s\n' 'Не найден .env.qualification; выполните make worktree-init.' >&2; exit 2; }

compose-context: ## Show the automatic project name and linked-worktree ports.
	@$(COMPOSE_CONTEXT)

compose-preflight: compose-env ## Check ports without starting or changing Docker resources.
	@$(COMPOSE_PREFLIGHT)

qualification-provision: compose-env compose-preflight ## Provision disposable qualification authority.
	$(COMPOSE_COMMAND) --profile qualification-provisioning run --rm qualification-authority-provisioner

start: qualification-provision ## Build and start the complete local Compose stack.
	$(COMPOSE_COMMAND) up --build --detach --wait

start-fixtures: qualification-provision ## Start the fixture-only web build and its backend dependencies.
	$(COMPOSE_COMMAND) --profile fixtures up --build --detach --wait web-fixtures

status: compose-env ## Show all local Compose services.
	$(COMPOSE_COMMAND) --profile fixtures ps --all

logs: compose-env ## Show recent logs; override LOG_TAIL or LOG_SERVICES if needed.
	$(COMPOSE_COMMAND) logs --tail="$(LOG_TAIL)" $(LOG_SERVICES)

stop: compose-env ## Stop the stack without deleting disposable volumes.
	$(COMPOSE_COMMAND) --profile fixtures down

destroy: compose-env ## Delete the selected Compose project and its disposable volumes.
	@$(COMPOSE_CONTEXT)
	@printf '%s\n' 'Удаляются контейнеры и все disposable volumes показанного Compose-проекта.'
	$(COMPOSE_COMMAND) --profile fixtures down --volumes --remove-orphans

api: ## Start the development API; override PORT if needed.
	$(UV) run uvicorn spine.api.main:app --reload --port $(PORT)
