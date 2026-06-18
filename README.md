# 🏮 SinoNom Vietnamese Document Intelligence Pipeline (HVH_004)

[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code Style: Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Type Checked: Mypy](https://img.shields.io/badge/mypy-checked-blue.svg)](http://mypy-lang.org/)
[![Testing: Pytest](https://img.shields.io/badge/tests-passed-green.svg)](https://docs.pytest.org/)

An end-to-end Document Intelligence & Natural Language Processing pipeline for the historical Sino-Vietnamese text **"An Nam Nhất Thống Chí" (安南一統志)**. This project implements state-of-the-art Chinese/Nom layout analysis, PaddleOCR text extraction, automated dictionary-based Hán-Việt transliteration, and sequence-level alignment validation.

Designed and developed according to the midterm requirements specified by **Prof. Dien (HCMUS NLP Lab)**.

---

## 🗺️ Architectural Pipeline

```text
 ┌──────────────┐      ┌─────────────┐      ┌─────────────────────────┐
 │  Landing URL │ ───> │ data_scraper│ ───> │     Raw Page Images     │
 └──────────────┘      └─────────────┘      └─────────────────────────┘
                                                         │
                                                         ▼
 ┌──────────────┐      ┌─────────────┐      ┌─────────────────────────┐
 │ ocr_layout   │ <─── │ spatial_    │ <─── │        PaddleOCR        │
 │ _output.json │      │ layout      │      │       Inference         │
 └──────────────┘      └─────────────┘      └─────────────────────────┘
        │
        ▼
 ┌──────────────┐      ┌─────────────┐      ┌─────────────────────────┐
 │  hanviet.csv │ ───> │  alignment_ │ ───> │  HVH_004_corpus.xml     │
 │ (11,214 words)      │  validator  │      │  HVH_004_alignment.xlsx │
 └──────────────┘      └─────────────┘      └─────────────────────────┘
```

---

## ✨ Key Features

*   **⚡ High-Performance Scraper**: Fully asynchronous image scraper (`aiohttp`/`aiofiles`) designed with robust exponential backoff, rate throttling, and smart caching to download 107 manuscript pages from the Nom Foundation library.
*   **📐 Right-to-Left Layout Analysis**: A custom spatial layout engine that clusters loose character bounding boxes into vertical columns reading from right to left using **Adaptive Horizontal Thresholding (AHT)**.
*   **📖 Dynamic Hán-Việt Transliteration**: Automatic integration of a **11,214-word Hán-Việt dictionary database** (`hanviet.csv`) to translate OCR-recognized Chinese characters into Quoc Ngu syllables.
*   **✅ S1 ∩ S2 Alignment Validation**: Multi-level alignment using Levenshtein edit-distance dynamic programming, validating characters using visual similarity ($S_1$) and Quoc Ngu translations ($S_2$) to automatically correct OCR errors.
*   **📊 Structured Outputs**: Outputs a clean, schema-compliant XML corpus and a multi-sheet color-coded Excel alignment map directly synced with Google Drive.
*   **🎨 Interactive Visual Playground**: A dedicated Jupyter Notebook playground to overlay layout columns and bounding boxes on the original scanned pages with full macOS CJK font fallbacks.

---

## 📁 Repository Structure

```text
SinoNomVietnamese_OCR_Project/
├── pyproject.toml                  # Modern package management metadata (PEP 621)
├── uv.lock                         # Exact pinned dependency lockfile
├── Makefile                        # Convenient development command aliases
├── src/
│   └── sinonom_ocr/
│       ├── data_scraper.py         # Async Nom Foundation scraper
│       ├── spatial_layout_engine.py# AHT clustering & BoundingBox structures
│       └── alignment_validator.py  # S1∩S2 edit-script validation engine
├── notebooks/
│   ├── 01_data_scraper.ipynb       # Phase 1: Scraper notebook
│   ├── 02_ocr_and_layout.ipynb     # Phase 2: OCR & Layout Analysis
│   ├── 03_alignment_and_export.ipynb # Phase 3: Alignment, Validation & Export
│   └── 04_visual_playground.ipynb  # Interactive visualization playground
├── tests/
│   └── test_pipeline.py            # Complete pytest suite
└── docs/
    ├── MidTerm_Requirement.pdf
    └── SinoNom_OCR_TransliterationAlignment.pdf
```

---

## 🧮 S1 ∩ S2 Character Validation Rules

For each OCR-recognized character `sn` and its corresponding Quoc Ngu word `qn`:

| Condition | Verdict | Status Color | Description |
| :--- | :--- | :---: | :--- |
| $sn \in S_2(qn)$ | Correct | ⚫ **BLACK** | OCR character matches translation directly. |
| $S_1(sn) \cap S_2(qn) \neq \emptyset$ | Corrected | 🟢 **GREEN** | OCR error corrected to the best match. |
| $S_1(sn) \cap S_2(qn) = \emptyset$ | OCR Failure | 🔴 **RED** | Complete mismatch or uncorrectable error. |

*   **S1**: Visual similarity set (from `SinoNom_Similar.dic`).
*   **S2**: Translation candidate set (from `QuocNgu_SinoNom.dic` + `hanviet.csv`).

---

## 🚀 Reproduction Guideline & Quick Start

> **Note**: A separate reproduction document is not required as all steps are fully automated via the `Makefile` and managed using the ultra-fast Python package manager `uv`.

### Prerequisites
*   **macOS** (supports local CPU execution) or **Linux/Windows**
*   **Jupyter Notebook / JupyterLab**
*   [uv](https://docs.astral.sh/uv/) installed:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

### 1. Installation
Clone the repository and install all development, testing, and notebook dependencies:
```bash
git clone https://github.com/khang3004/SinoNomViet_Transliteration_OCR.git
cd SinoNomViet_Transliteration_OCR
make install
```

### 2. Register Jupyter Kernel
Register the locked virtual environment as a selectable Jupyter kernel:
```bash
make kernel-install
```

### 3. Verify Code Quality & Run Tests
Ensure all unit tests and static code quality checks pass successfully:
```bash
make test    # Run all 12 test suites (geometry, layout, alignment)
make check   # Run Ruff formatter/linter and MyPy type check
```

### 4. Execute the End-to-End Pipeline
Run the entire 3-phase pipeline headlessly. This downloads/verifies the 107 page images, performs OCR + AHT clustering, runs transliteration/alignment, and generates the reports:
```bash
make notebook-run
```
*Outputs will be saved in `output/xml/` and `output/excel/` (and automatically synced to Google Drive if available).*

### 5. Interactive Visual Exploration
Launch Jupyter Lab to explore the layouts, bounding boxes, and text outputs interactively:
```bash
make notebook
# Open notebooks/04_visual_playground.ipynb
```

---

## 📊 Export Outputs

1.  **Corpus XML (`output/xml/HVH_004_corpus.xml`)**:
    Contains page-by-page, column-by-column, and character-by-character alignment datasets with standard structural tags.
2.  **Alignment Spreadsheet (`output/excel/HVH_004_alignment_map.xlsx`)**:
    *   **Sheet 1**: Sentence/column summary (Sentence ID, Hán Tự, Quốc Ngữ, Edit Distance, Accuracy, Status).
    *   **Sheet 2**: Character-by-character alignment detail (BLACK, GREEN, RED color-coded).
    *   **Sheet 3**: Built-in dictionary references.

---

## 🎓 References
*   *MidTerm_Requirement.pdf* — Midterm requirements (NLP Class, HCMUS).
*   *SinoNom_OCR_TransliterationAlignment.pdf* — Character alignment algorithm (Prof. Dien, HCMUS).
*   [Nom Foundation Digital Library](https://lib.nomfoundation.org) (Volume 664).
