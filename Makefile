# Reachy Mini <-> Claude Code connector.
# Run `make` (or `make help`) to see everything you can do.

# Optional overrides, e.g.:  make run EFFORT=low MODEL=sonnet PERMISSION=dontAsk
# Leave unset to use whatever server/.env says (an empty env var would override it).
MODEL      ?=
EFFORT     ?=
PERMISSION ?=

# Only pass these through when actually provided, so server/.env stays authoritative
# by default and an explicit `make run EFFORT=...` still wins.
RUN_ENV :=
ifneq ($(strip $(MODEL)),)
RUN_ENV += CLAUDE_MODEL=$(MODEL)
endif
ifneq ($(strip $(EFFORT)),)
RUN_ENV += CLAUDE_EFFORT=$(EFFORT)
endif
ifneq ($(strip $(PERMISSION)),)
RUN_ENV += CLAUDE_PERMISSION_MODE=$(PERMISSION)
endif

.DEFAULT_GOAL := help

.PHONY: help run run-robot server test stop logs health install

help: ## Show this help
	@echo "Reachy Mini <-> Claude connector"
	@echo
	@echo "Usage: make <target> [MODEL=opus|sonnet|haiku] [EFFORT=low|medium|high|xhigh|max] [PERMISSION=auto|dontAsk|plan]"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Examples:"
	@echo "  make run                 # start everything; open the printed URL on your phone"
	@echo "  make run EFFORT=low      # snappier voice replies"
	@echo "  make run MODEL=sonnet    # use a lighter/faster model"
	@echo "  make run PERMISSION=dontAsk  # read-only posture (no command execution)"
	@echo "  make run-robot           # run on the actual Reachy Mini"

run: ## Start connector + app (local: Mac mic/speaker). Ctrl-C stops both.
	@$(RUN_ENV) ./run.sh

run-robot: ## Start connector + app on the real Reachy Mini (--backend reachy)
	@$(RUN_ENV) ./run.sh --backend reachy

server: ## Start ONLY the connector server (the brain), for standalone testing
	@cd server && . .venv/bin/activate && \
		PIPER_MODEL=$${PIPER_MODEL:-$(CURDIR)/voices/bg_BG-dimitar-medium.onnx} \
		$(RUN_ENV) uvicorn main:app --host 0.0.0.0 --port 8080

test: ## Run reachy_app smoke tests (needs the connector running: make server)
	@. reachy_app/.venv/bin/activate && python -m reachy_app.tests.test_smoke

health: ## Show the connector's current model/effort/tools
	@curl -s localhost:8080/health | python3 -m json.tool || echo "connector not running"

logs: ## Tail the live logs (what Reachy heard / replied)
	@tail -f /tmp/run.log

stop: ## Stop the connector + app
	@pkill -f "run.sh" 2>/dev/null || true; \
	 pkill -f "uvicorn main:app" 2>/dev/null || true; \
	 pkill -f "reachy_app.main" 2>/dev/null || true; \
	 echo "stopped."

install: ## Create both venvs and install dependencies
	@echo "→ server venv"; \
	 python3 -m venv server/.venv && . server/.venv/bin/activate && \
		pip install -q -r server/requirements.txt && deactivate; \
	 echo "→ reachy_app venv"; \
	 python3 -m venv reachy_app/.venv && . reachy_app/.venv/bin/activate && \
		pip install -q -r reachy_app/requirements.txt && deactivate; \
	 echo "done. (macOS local audio also needs: brew install portaudio)"
