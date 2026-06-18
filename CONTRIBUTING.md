# Contributing to SinoNom OCR Pipeline

First off — **thank you** for taking the time to contribute! 🎉
This project is an open academic research effort and every contribution, from
a one-line doc fix to a new alignment algorithm, makes a real difference.

---

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [How Can I Contribute?](#how-can-i-contribute)
  - [Reporting Bugs](#reporting-bugs)
  - [Suggesting Enhancements](#suggesting-enhancements)
  - [Extending Dictionaries](#extending-dictionaries)
  - [Submitting Pull Requests](#submitting-pull-requests)
- [Development Setup](#development-setup)
- [Coding Standards](#coding-standards)
- [Commit Message Convention](#commit-message-convention)
- [Project Structure Overview](#project-structure-overview)

---

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
By participating, you are expected to uphold this code.
Please report unacceptable behaviour to the maintainers.

---

## How Can I Contribute?

### Reporting Bugs

Before filing a bug, please check the [open issues](../../issues) to see if it
has already been reported.

When submitting a bug report, include:

1. **Environment** — OS, Python version (`python --version`), `uv --version`
2. **Minimal reproduction** — smallest possible code / command that triggers the bug
3. **Expected vs actual behaviour**
4. **Full traceback** (paste inside a code block)

> Use the **Bug Report** issue template when available.

### Suggesting Enhancements

Feature requests are very welcome! Please open a **Feature Request** issue with:

- A clear and descriptive title
- The problem it solves / motivation
- Proposed approach (optional but helpful)
- Any academic references (paper DOI, book chapter, etc.)

### Extending Dictionaries

The S1 (`SinoNom_Similar.dic`) and S2 (`QuocNgu_SinoNom.dic`) dictionaries are
the heart of the alignment engine. To expand them:

1. Fork the repo and create a branch: `git checkout -b dict/expand-s1-radicals`
2. Edit `data/dicts/SinoNom_Similar.dic` or `data/dicts/QuocNgu_SinoNom.dic`
3. Follow the existing format strictly:
   ```
   # Comment lines start with '#'
   <char>:<similar1> <similar2> ...    # for S1
   <quoc_ngu>:<char1> <char2> ...      # for S2
   ```
4. Add a test in `tests/` that exercises the new entries
5. Open a PR with a clear description of the source / rationale for each entry

### Submitting Pull Requests

1. **Fork** the repo and create a feature branch from `main`:
   ```bash
   git checkout -b feat/my-new-feature
   ```
2. **Set up the dev environment** (see below)
3. **Write tests** for any new behaviour (`tests/` directory, `pytest`)
4. **Ensure the full quality suite passes**:
   ```bash
   make check
   ```
5. **Commit** using the [Conventional Commits](#commit-message-convention) style
6. **Push** and open a **Pull Request** against `main`
7. Fill in the PR template — describe the change, link any related issues

> **Draft PRs are welcome!** Open one early if you want feedback before finishing.

---

## Development Setup

> **Requirement:** [`uv`](https://docs.astral.sh/uv/) — a fast, modern Python
> package manager. Install it with:
> ```bash
> curl -LsSf https://astral.sh/uv/install.sh | sh
> ```

```bash
# 1. Clone the repository
git clone https://github.com/<your-org>/sinonom-ocr.git
cd sinonom-ocr

# 2. Create the virtual environment and install ALL dependencies (incl. dev)
make install          # runs: uv sync

# 3. Install pre-commit hooks (auto-runs ruff + mypy on every commit)
make hooks            # runs: uv run pre-commit install

# 4. Activate the environment (optional — make targets do this for you)
source .venv/bin/activate

# 5. Run the test suite
make test

# 6. Run linting + type-checking
make lint
```

---

## Coding Standards

| Tool | Purpose | Config |
|------|---------|--------|
| **Ruff** | Linting + formatting | `[tool.ruff]` in `pyproject.toml` |
| **mypy** | Static type checking | `[tool.mypy]` in `pyproject.toml` |
| **pytest** | Unit + async tests | `[tool.pytest.ini_options]` |

**All code must:**
- Use strict **type hints** on every function signature
- Include a **Google-style docstring** on every public function, class, and method
- Pass `ruff check` and `ruff format --check` with zero warnings
- Pass `mypy` with `--strict`

Run `make check` before every commit to confirm all of the above.

---

## Commit Message Convention

We follow [**Conventional Commits**](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short summary>

[optional body]

[optional footer(s)]
```

| Type | When to use |
|------|------------|
| `feat` | New feature / algorithm |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `dict` | Dictionary data additions / corrections |
| `refactor` | Code refactoring (no behaviour change) |
| `test` | Adding or fixing tests |
| `chore` | Build system, CI, dependencies |
| `perf` | Performance improvement |

**Examples:**
```
feat(alignment): add tie-breaking by S1 rank for multi-candidate GREEN hits
fix(scraper): handle 429 rate-limit with exponential back-off
dict(s2): add 250 new trăm/năm/thân entries from Kim Tự Điển v3
docs(readme): add Google Colab badge and quickstart GIF
```

---

## Project Structure Overview

```
sinonom-ocr/
├── data_scraper.py           # Module 1 — async image downloader
├── spatial_layout_engine.py  # Module 2 — RTL column clustering
├── alignment_validator.py    # Module 3 — S1∩S2 Levenshtein alignment
├── hvm_dataset_generator.ipynb  # Module 4 — master pipeline notebook
│
├── tests/                    # pytest test suite
│   ├── test_scraper.py
│   ├── test_layout.py
│   └── test_alignment.py
│
├── data/dicts/               # Dictionary files (S1 + S2)
├── output/                   # Generated XML and Excel outputs
├── docs/                     # Academic reference PDFs
│
├── pyproject.toml            # Project metadata + tool config
├── Makefile                  # Developer command shortcuts
├── .pre-commit-config.yaml   # Git hook configuration
└── .github/
    ├── workflows/ci.yml      # GitHub Actions CI pipeline
    └── ISSUE_TEMPLATE/
```

---

## Questions?

Open a [Discussion](../../discussions) — we're happy to help!
