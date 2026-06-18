# ─────────────────────────────────────────────────────────────────────────────
# SinoNom OCR Pipeline — Makefile
# Convenient command aliases for uv-based development workflow.
#
# Usage:   make <target>
# Requires: uv (https://docs.astral.sh/uv/)
# ─────────────────────────────────────────────────────────────────────────────

SHELL := /bin/bash
.DEFAULT_GOAL := help

# ── Colours ──────────────────────────────────────────────────────────────────
CYAN  := \033[0;36m
GREEN := \033[0;32m
YELLOW:= \033[0;33m
RESET := \033[0m

# ── Paths ─────────────────────────────────────────────────────────────────────
PYTHON      := uv run python
PIP         := uv pip
PYTEST      := uv run pytest
RUFF        := uv run ruff
MYPY        := uv run mypy
JUPYTER     := uv run jupyter
NBCONVERT   := uv run jupyter nbconvert

RAW_IMAGES  := data/raw_images
OUTPUT_DIR  := output
NOTEBOOK    := hvm_dataset_generator.ipynb

# ─────────────────────────────────────────────────────────────────────────────
.PHONY: help install install-dev sync update hooks \
        lint format typecheck check test test-cov \
        scrape layout align run notebook clean distclean \
        kernel-install

# ─────────────────────────────────────────────────────────────────────────────
help:  ## Show this help message
	@echo ""
	@echo "  $(CYAN)SinoNom OCR Pipeline$(RESET) — available make targets"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ─────────────────────────────────────────────────────────────────────────────
# Environment management
# ─────────────────────────────────────────────────────────────────────────────
install:  ## Create .venv and install all runtime + dev deps via uv
	uv sync
	@echo "$(GREEN)✅ Environment ready at .venv$(RESET)"

install-dev:  ## Install only dev extras (linters, tests, notebooks)
	uv sync --extra dev --extra notebook
	@echo "$(GREEN)✅ Dev environment ready$(RESET)"

sync:  ## Re-sync .venv with current pyproject.toml (fast)
	uv sync
	@echo "$(GREEN)✅ Synced$(RESET)"

update:  ## Upgrade all dependencies to latest compatible versions
	uv lock --upgrade
	uv sync
	@echo "$(GREEN)✅ Dependencies updated$(RESET)"

hooks:  ## Install pre-commit git hooks (ruff + mypy run on every commit)
	uv run pre-commit install
	@echo "$(GREEN)✅ Pre-commit hooks installed$(RESET)"

kernel-install:  ## Register .venv as a named Jupyter kernel
	uv run python -m ipykernel install --user \
		--name "sinonom-ocr" \
		--display-name "SinoNom OCR (uv .venv)"
	@echo "$(GREEN)✅ Kernel 'sinonom-ocr' registered — select it in Jupyter$(RESET)"

# ─────────────────────────────────────────────────────────────────────────────
# Code quality
# ─────────────────────────────────────────────────────────────────────────────
lint:  ## Run ruff linter (check only, no auto-fix)
	$(RUFF) check .
	@echo "$(GREEN)✅ Linting passed$(RESET)"

format:  ## Auto-format code with ruff
	$(RUFF) format .
	$(RUFF) check --fix .
	@echo "$(GREEN)✅ Formatting done$(RESET)"

typecheck:  ## Run mypy static type checker
	$(MYPY) src/sinonom_ocr/
	@echo "$(GREEN)✅ Type check passed$(RESET)"

check: lint typecheck  ## Run full quality suite (lint + types)
	@echo "$(GREEN)✅ All quality checks passed$(RESET)"

# ─────────────────────────────────────────────────────────────────────────────
# Testing
# ─────────────────────────────────────────────────────────────────────────────
test:  ## Run the test suite with pytest
	$(PYTEST) tests/ -v

test-cov:  ## Run tests with HTML coverage report
	$(PYTEST) tests/ --cov=. --cov-report=html --cov-report=term-missing
	@echo "$(CYAN)Coverage report:$(RESET) open htmlcov/index.html"

# ─────────────────────────────────────────────────────────────────────────────
# Pipeline execution
# ─────────────────────────────────────────────────────────────────────────────
scrape:  ## Download all page images from the Nom Foundation (set URL in env)
	$(PYTHON) -m sinonom_ocr.data_scraper \
		--url "https://lib.nomfoundation.org/collection/1/volume/664/" \
		--out $(RAW_IMAGES) \
		--workers 6 \
		--delay 1.0 \
		--verbose

layout:  ## Run spatial layout engine smoke-test
	$(PYTHON) -m sinonom_ocr.spatial_layout_engine

align:  ## Run alignment validator smoke-test
	$(PYTHON) -m sinonom_ocr.alignment_validator

run: layout align  ## Run all pipeline module smoke-tests
	@echo "$(GREEN)✅ All modules OK$(RESET)"

# ─────────────────────────────────────────────────────────────────────────────
# Notebook
# ─────────────────────────────────────────────────────────────────────────────
notebook:  ## Launch Jupyter Lab with the project notebooks
	$(JUPYTER) lab notebooks/

notebook-run:  ## Execute all notebooks end-to-end (non-interactive)
	@mkdir -p output/executed_notebooks
	$(NBCONVERT) --to notebook --execute notebooks/01_data_scraper.ipynb --output-dir output/executed_notebooks --output 01_data_scraper.ipynb --ExecutePreprocessor.timeout=300
	$(NBCONVERT) --to notebook --execute notebooks/02_ocr_and_layout.ipynb --output-dir output/executed_notebooks --output 02_ocr_and_layout.ipynb --ExecutePreprocessor.timeout=300
	$(NBCONVERT) --to notebook --execute notebooks/03_alignment_and_export.ipynb --output-dir output/executed_notebooks --output 03_alignment_and_export.ipynb --ExecutePreprocessor.timeout=300
	@echo "$(GREEN)✅ All notebooks executed successfully!$(RESET)"

# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────
clean:  ## Remove generated output files (XML, Excel, pycache)
	rm -rf output/xml/*.xml output/excel/*.xlsx
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
	@echo "$(YELLOW)🧹 Output files cleaned$(RESET)"

distclean: clean  ## Remove .venv, uv.lock, and all generated artefacts
	rm -rf .venv uv.lock htmlcov .coverage coverage.xml .mypy_cache .ruff_cache
	@echo "$(YELLOW)🧹 Full clean done (run 'make install' to rebuild)$(RESET)"
