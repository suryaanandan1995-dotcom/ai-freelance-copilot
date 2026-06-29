# AI Freelance Copilot — developer tasks
.DEFAULT_GOAL := help
.PHONY: help install test lint build-kb run dashboard mcp stats docker

IMAGE ?= ghcr.io/suryaanandan1995-dotcom/ai-freelance-copilot:latest

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies into the active environment
	pip install -r requirements.txt

test: ## Run the offline test suite
	pytest -q

lint: ## Lint with ruff
	ruff check .

build-kb: ## (Re)build the portfolio RAG knowledge base
	python -m scripts.build_kb

run: ## Run one pipeline pass (discover -> qualify -> research -> draft -> queue)
	python main.py run

dashboard: ## Serve the human approval dashboard on :8000
	python main.py dashboard --host 0.0.0.0 --port 8000

mcp: ## Run the MCP stdio server
	python main.py mcp

stats: ## Print pipeline stats
	python main.py stats

docker: ## Build the container image
	docker build -t $(IMAGE) .
