# Everything CI runs, runnable by hand in the same way.
#
# The point of a Makefile here rather than a list of commands in a README is
# that the build and the person get the same behaviour out of one definition.
# `make check` is what a change has to pass.

DUMP     := catalogue-dump
CONTROL  := catalogue-control
SERVICE  := catalogue-service
EXPLORER := catalogue-explorer

# `--directory` also changes the working directory, so every path below is
# relative to the project it names. VIRTUAL_ENV is cleared because an activated
# environment elsewhere in the tree is not this project's, and uv would
# otherwise warn about it on every single invocation.
UV       := VIRTUAL_ENV= uv --directory $(DUMP)
RUN      := $(UV) run --
UVC      := VIRTUAL_ENV= uv --directory $(CONTROL)
RUNC     := $(UVC) run --
UVS      := VIRTUAL_ENV= uv --directory $(SERVICE)
RUNS     := $(UVS) run --

.DEFAULT_GOAL := help

.PHONY: help
help:  ## List the targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install:  ## Sync every project's virtualenv, including dev groups
	$(UV) sync --all-groups
	$(UVC) sync --all-groups
	$(UVS) sync --all-groups
	cd $(EXPLORER) && npm install

.PHONY: lint
lint:  ## ruff, across all three Python projects
	$(RUN) ruff check .
	$(RUNC) ruff check .
	$(RUNS) ruff check .

.PHONY: format
format:  ## ruff, fixing what it can
	$(RUN) ruff check --fix .
	$(RUNC) ruff check --fix .
	$(RUNS) ruff check --fix .

.PHONY: typecheck
typecheck:  ## mypy, and svelte-check for the explorer
	$(RUN) mypy
	$(RUNC) mypy
	$(RUNS) mypy
	cd $(EXPLORER) && npm run check

.PHONY: test
test:  ## The fast suites: no network, no database, no cache replay
	$(RUN) pytest
	$(RUNC) pytest
	$(RUNS) pytest

.PHONY: test-golden
test-golden:  ## Replay every cached source and compare against its frozen dump
	$(RUN) pytest -m golden

.PHONY: golden-update
golden-update:  ## Rewrite the frozen dumps. Review the diff; it is the change.
	$(RUN) pytest -m golden --update-golden

# A throwaway server on a port of its own, so a run cannot touch the development
# database on 5434. The tests drop and recreate the `catalogue` schema, which is
# not something to point at anything that matters.
PGTEST_PORT ?= 55432
PGTEST_DSN  ?= postgresql://postgres:postgres@127.0.0.1:$(PGTEST_PORT)/postgres

.PHONY: pg-up
pg-up:  ## Start the throwaway PostgreSQL the database tests need
	@docker run -d --rm --name catalogue-pgtest \
	  -e POSTGRES_PASSWORD=postgres -p 127.0.0.1:$(PGTEST_PORT):5432 \
	  postgres:17-alpine >/dev/null
	@until docker exec catalogue-pgtest pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
	@echo "catalogue-pgtest listening on $(PGTEST_PORT)"

.PHONY: pg-down
pg-down:  ## Stop it
	@docker stop catalogue-pgtest >/dev/null 2>&1 || true

.PHONY: test-postgres
test-postgres:  ## Database-backed tests: the queue, edges, run closure, the API
	CATALOGUE_TEST_DSN=$(PGTEST_DSN) $(RUN) pytest -m postgres
	CATALOGUE_TEST_DSN=$(PGTEST_DSN) $(RUNC) pytest -m postgres

# -- generated contracts -----------------------------------------------------
#
# Never hand-edit the generated documents. Change the Pydantic registries, run
# `make openapi`, and commit the diff — which is what makes an API change
# visible in the review of the pull request that makes it.

.PHONY: openapi
openapi:  ## Regenerate both OpenAPI documents and the explorer's TypeScript
	$(RUNS) catalogue-openapi
	$(RUNC) catalogue-ops-openapi
	$(RUNC) catalogue-ops-types

.PHONY: openapi-check
openapi-check:  ## Fail if a generated contract has drifted from the code
	$(RUNS) catalogue-openapi --check
	$(RUNC) catalogue-ops-openapi --check
	$(RUNC) catalogue-ops-types --check

.PHONY: check
check: lint typecheck test openapi-check  ## What every change has to pass

.PHONY: check-all
check-all: check test-golden  ## check, the replay suite, and the database suite
	@$(MAKE) pg-up
	@$(MAKE) test-postgres; status=$$?; $(MAKE) pg-down; exit $$status
