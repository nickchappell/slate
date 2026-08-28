.PHONY: help install sync lint lint-fix format format-check test test-unit test-integration \
	check run update outdated lock bump-version clean

# Used by bump-version; override with e.g. `make bump-version PART=minor`.
PART ?= patch

# Colors for help/status output (disabled automatically when not writing to a terminal).
ifneq (,$(findstring xterm,${TERM})$(findstring color,${TERM}))
  BOLD   := \033[1m
  CYAN   := \033[36m
  GREEN  := \033[32m
  YELLOW := \033[33m
  RED    := \033[31m
  RESET  := \033[0m
endif

help:
	@printf "$(BOLD)Available targets:$(RESET)\n"
	@printf "  $(CYAN)install$(RESET)          Install/sync project + dev dependencies (uv sync)\n"
	@printf "                   $(GREEN)e.g. make install$(RESET)\n"
	@printf "  $(CYAN)lint$(RESET)             Run ruff check\n"
	@printf "                   $(GREEN)e.g. make lint$(RESET)\n"
	@printf "  $(CYAN)lint-fix$(RESET)         Run ruff check --fix\n"
	@printf "                   $(GREEN)e.g. make lint-fix$(RESET)\n"
	@printf "  $(CYAN)format$(RESET)           Run ruff format\n"
	@printf "                   $(GREEN)e.g. make format$(RESET)\n"
	@printf "  $(CYAN)format-check$(RESET)     Run ruff format --check\n"
	@printf "                   $(GREEN)e.g. make format-check$(RESET)\n"
	@printf "  $(CYAN)test$(RESET)             Run unit tests (excludes tests/integration)\n"
	@printf "                   $(GREEN)e.g. make test$(RESET)\n"
	@printf "  $(CYAN)test-integration$(RESET) Run integration tests only\n"
	@printf "                   $(GREEN)e.g. make test-integration$(RESET)\n"
	@printf "  $(CYAN)check$(RESET)            lint + format-check + test (pre-commit sanity)\n"
	@printf "                   $(GREEN)e.g. make check$(RESET)\n"
	@printf "  $(CYAN)run$(RESET) ARGS=...     Run the CLI\n"
	@printf "                   $(GREEN)e.g. make run ARGS='--dry-run --input-dir footage'$(RESET)\n"
	@printf "  $(CYAN)update$(RESET)           Upgrade all dependencies and refresh uv.lock\n"
	@printf "                   $(GREEN)e.g. make update$(RESET)\n"
	@printf "  $(CYAN)outdated$(RESET)         Show dependencies with newer versions available\n"
	@printf "                   $(GREEN)e.g. make outdated$(RESET)\n"
	@printf "  $(CYAN)lock$(RESET)             Regenerate uv.lock without upgrading anything\n"
	@printf "                   $(GREEN)e.g. make lock$(RESET)\n"
	@printf "  $(CYAN)bump-version$(RESET)     Bump pyproject.toml version, commit, and tag it\n"
	@printf "                   $(YELLOW)(PART=patch|minor|major, default patch)$(RESET)\n"
	@printf "                   $(GREEN)e.g. make bump-version PART=minor$(RESET)\n"
	@printf "  $(CYAN)clean$(RESET)            Remove caches and build artifacts\n"
	@printf "                   $(GREEN)e.g. make clean$(RESET)\n"

install sync:
	uv sync

lint:
	uv run ruff check .

lint-fix:
	uv run ruff check --fix .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

test test-unit:
	uv run pytest tests/ --ignore=tests/integration

test-integration:
	uv run pytest tests/integration -m integration

check: lint format-check test

run:
	uv run slate $(ARGS)

update:
	uv lock --upgrade
	uv sync

outdated:
	uv tree --outdated

lock:
	uv lock

bump-version:
	@if [ -n "$$(git status --porcelain)" ]; then \
		printf "$(RED)Working tree not clean -- commit or stash changes before bumping the version.$(RESET)\n" >&2; \
		exit 1; \
	fi
	uv version --bump $(PART)
	@NEW_VERSION=$$(uv version --short); \
	git add pyproject.toml uv.lock; \
	git commit -m "Bump version to $$NEW_VERSION"; \
	git tag "v$$NEW_VERSION"; \
	printf "$(GREEN)Tagged v$$NEW_VERSION$(RESET) -- push with: $(CYAN)git push && git push origin v$$NEW_VERSION$(RESET)\n"

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info
	find . -type d -name '__pycache__' -exec rm -rf {} +
