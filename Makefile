.PHONY: help install sync lint lint-fix format format-check fix test test-unit test-integration \
	test-all coverage coverage-report check audit run model-update-check build install-tool \
	reinstall-tool uninstall-tool update outdated lock lock-check bump-version github-release \
	create-release clean

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
	@printf "  $(CYAN)fix$(RESET)              Auto-fix everything: ruff check --fix + ruff format\n"
	@printf "                   $(GREEN)e.g. make fix$(RESET)\n"
	@printf "  $(CYAN)test$(RESET)             Run unit tests (excludes tests/integration)\n"
	@printf "                   $(GREEN)e.g. make test$(RESET)\n"
	@printf "  $(CYAN)test-integration$(RESET) Run integration tests only\n"
	@printf "                   $(GREEN)e.g. make test-integration$(RESET)\n"
	@printf "  $(CYAN)test-all$(RESET)         Run unit + integration tests together\n"
	@printf "                   $(GREEN)e.g. make test-all$(RESET)\n"
	@printf "  $(CYAN)coverage$(RESET)         Run unit tests with coverage (term + htmlcov/ + coverage.xml)\n"
	@printf "                   $(GREEN)e.g. make coverage$(RESET)\n"
	@printf "  $(CYAN)coverage-report$(RESET)  Regenerate term/HTML/XML reports from the last coverage run\n"
	@printf "                   $(GREEN)e.g. make coverage-report$(RESET)\n"
	@printf "  $(CYAN)check$(RESET)            lint + format-check + lock-check + test (pre-commit sanity)\n"
	@printf "                   $(GREEN)e.g. make check$(RESET)\n"
	@printf "  $(CYAN)audit$(RESET)            Scan dependencies for known vulnerabilities (pip-audit)\n"
	@printf "                   $(GREEN)e.g. make audit$(RESET)\n"
	@printf "  $(CYAN)run$(RESET) ARGS=...     Run the CLI\n"
	@printf "                   $(GREEN)e.g. make run ARGS='--dry-run --input-dir footage'$(RESET)\n"
	@printf "  $(CYAN)model-update-check$(RESET) Check the Hub for a newer model revision\n"
	@printf "                   $(GREEN)e.g. make model-update-check$(RESET)\n"
	@printf "  $(CYAN)build$(RESET)            Build the wheel + sdist into a fresh dist/ (uv build)\n"
	@printf "                   $(GREEN)e.g. make build$(RESET)\n"
	@printf "  $(CYAN)install-tool$(RESET)     Install slate on PATH as a uv tool (bytecode precompiled)\n"
	@printf "                   $(GREEN)e.g. make install-tool$(RESET)\n"
	@printf "  $(CYAN)reinstall-tool$(RESET)   Reinstall the slate uv tool from the current source (bytecode precompiled)\n"
	@printf "                   $(GREEN)e.g. make reinstall-tool$(RESET)\n"
	@printf "  $(CYAN)uninstall-tool$(RESET)   Remove the slate uv tool from PATH\n"
	@printf "                   $(GREEN)e.g. make uninstall-tool$(RESET)\n"
	@printf "  $(CYAN)update$(RESET)           Upgrade all dependencies and refresh uv.lock\n"
	@printf "                   $(GREEN)e.g. make update$(RESET)\n"
	@printf "  $(CYAN)outdated$(RESET)         Show dependencies with newer versions available\n"
	@printf "                   $(GREEN)e.g. make outdated$(RESET)\n"
	@printf "  $(CYAN)lock$(RESET)             Regenerate uv.lock without upgrading anything\n"
	@printf "                   $(GREEN)e.g. make lock$(RESET)\n"
	@printf "  $(CYAN)lock-check$(RESET)       Verify uv.lock is up to date with pyproject.toml\n"
	@printf "                   $(GREEN)e.g. make lock-check$(RESET)\n"
	@printf "  $(CYAN)bump-version$(RESET)     Bump pyproject.toml version, commit, and tag it\n"
	@printf "                   $(YELLOW)(PART=patch|minor|major, default patch)$(RESET)\n"
	@printf "                   $(GREEN)e.g. make bump-version PART=minor$(RESET)\n"
	@printf "  $(CYAN)github-release$(RESET)   Create a GitHub release for the current version's tag\n"
	@printf "                   $(YELLOW)(requires the tag to already be pushed to origin)$(RESET)\n"
	@printf "                   $(GREEN)e.g. make github-release$(RESET)\n"
	@printf "  $(CYAN)create-release$(RESET)   bump-version, push it, then github-release, end to end\n"
	@printf "                   $(YELLOW)(PART=patch|minor|major, default patch)$(RESET)\n"
	@printf "                   $(GREEN)e.g. make create-release PART=minor$(RESET)\n"
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

fix: lint-fix format

test test-unit:
	uv run pytest tests/ --ignore=tests/integration

test-integration:
	uv run pytest tests/integration -m integration

test-all:
	uv run pytest tests/

coverage:
	uv run pytest tests/ --ignore=tests/integration \
		--cov=slate --cov-report=term-missing --cov-report=html --cov-report=xml

coverage-report:
	uv run coverage report --show-missing
	uv run coverage html
	uv run coverage xml

check: lint format-check lock-check test

audit:
	uv run --with pip-audit pip-audit

run:
	uv run slate $(ARGS)

model-update-check:
	uv run slate --model-update-check

build:
	rm -rf dist
	uv build

install-tool:
	uv tool install --compile-bytecode .

reinstall-tool:
	uv tool install --reinstall --compile-bytecode .

uninstall-tool:
	uv tool uninstall slate

update:
	uv lock --upgrade
	uv sync

outdated:
	uv tree --outdated

lock:
	uv lock

lock-check:
	uv lock --check

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

github-release:
	@if ! command -v gh >/dev/null 2>&1; then \
		printf "$(RED)gh CLI not found -- install it first (e.g. brew install gh).$(RESET)\n" >&2; \
		exit 1; \
	fi
	@if ! gh auth status >/dev/null 2>&1; then \
		printf "$(RED)gh is not authenticated -- run 'gh auth login' first.$(RESET)\n" >&2; \
		exit 1; \
	fi
	@TAG="v$$(uv version --short)"; \
	printf "$(BOLD)Creating GitHub release $(CYAN)$$TAG$(RESET)$(BOLD)...$(RESET)\n"; \
	gh release create "$$TAG" --verify-tag --generate-notes --title "$$TAG" \
		|| { printf "$(RED)Release failed -- has tag $$TAG been pushed? Try: git push origin $$TAG$(RESET)\n" >&2; exit 1; }

create-release: bump-version
	@TAG="v$$(uv version --short)"; \
	printf "$(BOLD)Pushing $(CYAN)$$TAG$(RESET)$(BOLD) to origin...$(RESET)\n"; \
	git push && git push origin "$$TAG"
	$(MAKE) github-release

clean:
	rm -rf .pytest_cache .ruff_cache build dist *.egg-info htmlcov coverage.xml .coverage .coverage.*
	find . -type d -name '__pycache__' -exec rm -rf {} +
