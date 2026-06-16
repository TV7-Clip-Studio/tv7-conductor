VENV_DIR ?= .venv
PYTHON_FALLBACK ?= /opt/homebrew/bin/python3
PYTHON ?= $(if $(wildcard $(VENV_DIR)/bin/python),$(VENV_DIR)/bin/python,$(if $(wildcard $(PYTHON_FALLBACK)),$(PYTHON_FALLBACK),python3))
CONDUCTOR_REPO ?=
CONDUCTOR_REPO_PATH ?= $(CONDUCTOR_REPO)
CONDUCTOR_ITEM_ID ?=
CONDUCTOR_WORKTREE_ROOT ?= docs/.conductor/worktrees
CONDUCTOR_WORKTREE_BRANCH ?=
CONDUCTOR_WORKTREE_BASE ?= HEAD
CONDUCTOR_PREVIEW_PORT ?= 0
CONDUCTOR_PREVIEW_COMMAND ?=
CONDUCTOR_HEALTH_URL ?=
CONDUCTOR_WAIT_SEC ?= 10
CONDUCTOR_VALIDATION_COMMAND ?=
CONDUCTOR_VALIDATION_TIMEOUT ?= 600
CONDUCTOR_LOCAL_REVIEW_COMMAND ?= $(CONDUCTOR_VALIDATION_COMMAND)
CONDUCTOR_LOCAL_REVIEW_TIMEOUT ?= $(CONDUCTOR_VALIDATION_TIMEOUT)
CONDUCTOR_REVIEW_PROVIDER ?= deepseek_api_reviewer
CONDUCTOR_REVIEW_ALLOW_PAID ?= 0
CONDUCTOR_REVIEW_MAX_LOOPS ?= 0
CONDUCTOR_APPLY ?= 0
CONDUCTOR_WORKTREE_PATH ?=
CONDUCTOR_LEGACY_STATE_ROOT ?= ../tv7-core/docs/.conductor
CONDUCTOR_ACTIVE_STATE_ROOT ?= docs/.conductor

.PHONY: venv install-dev install-web precommit-context conductor-init conductor-doctor conductor-status conductor-plan conductor-run conductor-test conductor-prepare-worktree conductor-launch-preview conductor-validate-worktree conductor-local-review conductor-external-review conductor-review conductor-state-migration-check

venv:
	@$(PYTHON_FALLBACK) -m venv "$(VENV_DIR)"
	@"$(VENV_DIR)/bin/python" -m pip install --upgrade pip

install-dev: venv
	@"$(VENV_DIR)/bin/python" -m pip install -e .

install-web: venv
	@"$(VENV_DIR)/bin/python" -m pip install -e ".[web]"

conductor-init:
	@"$(PYTHON)" tooling/conductor.py init

conductor-doctor:
	@"$(PYTHON)" tooling/conductor.py doctor

conductor-status:
	@"$(PYTHON)" tooling/conductor.py status

conductor-plan:
	@"$(PYTHON)" tooling/conductor.py plan

conductor-run:
	@"$(PYTHON)" tooling/conductor_run.py

conductor-test:
	@PYTHONPATH=tooling "$(PYTHON)" -m unittest discover -s tooling/tests/orchestrator -p 'test_*.py'

precommit-context:
	@"$(PYTHON)" -m py_compile tooling/conductor.py tooling/conductor_run.py tooling/orchestrator/*.py
	@$(MAKE) conductor-test
	@git diff --check

conductor-prepare-worktree:
	@if [ -z "$(CONDUCTOR_REPO)" ] || [ -z "$(CONDUCTOR_ITEM_ID)" ]; then \
		echo "Set CONDUCTOR_REPO and CONDUCTOR_ITEM_ID. Override CONDUCTOR_REPO_PATH only when the repo path differs from the repo id." >&2; \
		exit 2; \
	fi
	@set -- "$(PYTHON)" tooling/conductor.py prepare-worktree --repo "$(CONDUCTOR_REPO)" --repo-path "$(CONDUCTOR_REPO_PATH)" --item-id "$(CONDUCTOR_ITEM_ID)" --worktree-root "$(CONDUCTOR_WORKTREE_ROOT)" --base-ref "$(CONDUCTOR_WORKTREE_BASE)" --preview-port "$(CONDUCTOR_PREVIEW_PORT)"; \
	if [ -n "$(CONDUCTOR_WORKTREE_BRANCH)" ]; then set -- "$$@" --branch "$(CONDUCTOR_WORKTREE_BRANCH)"; fi; \
	if [ -n "$(CONDUCTOR_PREVIEW_COMMAND)" ]; then set -- "$$@" --preview-command "$(CONDUCTOR_PREVIEW_COMMAND)"; fi; \
	if [ "$(CONDUCTOR_APPLY)" = "1" ]; then set -- "$$@" --apply; fi; \
	"$$@"

conductor-launch-preview:
	@if [ -z "$(CONDUCTOR_REPO)" ] || [ -z "$(CONDUCTOR_ITEM_ID)" ] || [ -z "$(CONDUCTOR_WORKTREE_PATH)" ] || [ -z "$(CONDUCTOR_PREVIEW_COMMAND)" ] || [ "$(CONDUCTOR_PREVIEW_PORT)" = "0" ]; then \
		echo "Set CONDUCTOR_REPO, CONDUCTOR_ITEM_ID, CONDUCTOR_WORKTREE_PATH, CONDUCTOR_PREVIEW_COMMAND, and CONDUCTOR_PREVIEW_PORT." >&2; \
		exit 2; \
	fi
	@set -- "$(PYTHON)" tooling/conductor.py launch-preview --repo "$(CONDUCTOR_REPO)" --repo-path "$(CONDUCTOR_REPO_PATH)" --item-id "$(CONDUCTOR_ITEM_ID)" --worktree-path "$(CONDUCTOR_WORKTREE_PATH)" --preview-port "$(CONDUCTOR_PREVIEW_PORT)" --preview-command "$(CONDUCTOR_PREVIEW_COMMAND)" --wait-sec "$(CONDUCTOR_WAIT_SEC)"; \
	if [ -n "$(CONDUCTOR_HEALTH_URL)" ]; then set -- "$$@" --health-url "$(CONDUCTOR_HEALTH_URL)"; fi; \
	"$$@"

conductor-validate-worktree:
	@if [ -z "$(CONDUCTOR_REPO)" ] || [ -z "$(CONDUCTOR_ITEM_ID)" ] || [ -z "$(CONDUCTOR_WORKTREE_PATH)" ] || [ -z "$(CONDUCTOR_VALIDATION_COMMAND)" ]; then \
		echo "Set CONDUCTOR_REPO, CONDUCTOR_ITEM_ID, CONDUCTOR_WORKTREE_PATH, and CONDUCTOR_VALIDATION_COMMAND." >&2; \
		exit 2; \
	fi
	@"$(PYTHON)" tooling/conductor.py validate-worktree --repo "$(CONDUCTOR_REPO)" --repo-path "$(CONDUCTOR_REPO_PATH)" --item-id "$(CONDUCTOR_ITEM_ID)" --worktree-path "$(CONDUCTOR_WORKTREE_PATH)" --preview-port "$(CONDUCTOR_PREVIEW_PORT)" --command "$(CONDUCTOR_VALIDATION_COMMAND)" --timeout-sec "$(CONDUCTOR_VALIDATION_TIMEOUT)"

conductor-local-review:
	@if [ -z "$(CONDUCTOR_REPO)" ] || [ -z "$(CONDUCTOR_LOCAL_REVIEW_COMMAND)" ]; then \
		echo "Set CONDUCTOR_REPO and CONDUCTOR_LOCAL_REVIEW_COMMAND. Set CONDUCTOR_WORKTREE_PATH to review an isolated worktree." >&2; \
		exit 2; \
	fi
	@set -- "$(PYTHON)" tooling/conductor.py local-review --repo "$(CONDUCTOR_REPO)" --repo-path "$(CONDUCTOR_REPO_PATH)" --command "$(CONDUCTOR_LOCAL_REVIEW_COMMAND)" --timeout-sec "$(CONDUCTOR_LOCAL_REVIEW_TIMEOUT)"; \
	if [ -n "$(CONDUCTOR_ITEM_ID)" ]; then set -- "$$@" --item-prefix "$(CONDUCTOR_ITEM_ID)"; fi; \
	if [ -n "$(CONDUCTOR_WORKTREE_PATH)" ]; then set -- "$$@" --worktree-path "$(CONDUCTOR_WORKTREE_PATH)"; fi; \
	if [ "$(CONDUCTOR_APPLY)" = "1" ]; then set -- "$$@" --apply; fi; \
	"$$@"

conductor-external-review:
	@if [ -z "$(CONDUCTOR_REPO)" ]; then \
		echo "Set CONDUCTOR_REPO. External review requires linked PRs and usually CONDUCTOR_REVIEW_ALLOW_PAID=1." >&2; \
		exit 2; \
	fi
	@set -- "$(PYTHON)" tooling/conductor.py external-review --repo "$(CONDUCTOR_REPO)" --reviewer-provider "$(CONDUCTOR_REVIEW_PROVIDER)" --max-loops "$(CONDUCTOR_REVIEW_MAX_LOOPS)"; \
	if [ -n "$(CONDUCTOR_ITEM_ID)" ]; then set -- "$$@" --item-prefix "$(CONDUCTOR_ITEM_ID)"; fi; \
	if [ "$(CONDUCTOR_REVIEW_ALLOW_PAID)" = "1" ]; then set -- "$$@" --allow-paid-review; fi; \
	if [ "$(CONDUCTOR_APPLY)" = "1" ]; then set -- "$$@" --apply; fi; \
	"$$@"

conductor-review: conductor-external-review

conductor-state-migration-check:
	@"$(PYTHON)" tooling/conductor.py state-migration-check --legacy-state-root "$(CONDUCTOR_LEGACY_STATE_ROOT)" --active-state-root "$(CONDUCTOR_ACTIVE_STATE_ROOT)"
