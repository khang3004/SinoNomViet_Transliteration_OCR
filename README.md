# SinoNom Vietnamese OCR Project — HVH_004

> **An Nam Nhất Thống Chí** | HCMUS NLP Midterm Project  
> **Prof. Dien** | HCMUS — Faculty of Information Technology

---

## Overview

End-to-end NLP pipeline for the historical Sino-Vietnamese text *An Nam Nhất Thống Chí* (安南一統志) from the [Nom Foundation Digital Library](https://lib.nomfoundation.org/collection/1/volume/664/).

The pipeline processes raw scanned manuscript images through OCR, spatial layout analysis, alignment validation, and exports structured XML and Excel datasets.

---

## Project Structure

```
SinoNomVietnamese_OCR_Project/
│
├── 📄 data_scraper.py              # Module 1: Async image downloader
├── 📐 spatial_layout_engine.py     # Module 2: Right-to-Left column clustering
├── ✅ alignment_validator.py       # Module 3: S1∩S2 Levenshtein alignment
├── 📓 hvm_dataset_generator.ipynb  # Module 4: Master pipeline notebook
│
├── requirements.txt                # Python dependencies
│
├── data/
│   ├── raw_images/                 # Downloaded page images (page_001.jpg, ...)
│   └── dicts/
│       ├── SinoNom_Similar.dic     # S1: Visual similarity dictionary
│       └── QuocNgu_SinoNom.dic     # S2: QN→SinoNom translation dictionary
│
├── output/
│   ├── xml/
│   │   └── HVH_004_corpus.xml      # Final structured XML output
│   └── excel/
│       └── HVH_004_alignment_map.xlsx  # Final Excel alignment map
│
└── docs/
    ├── MidTerm_Requirement.pdf
    └── SinoNom_OCR_TransliterationAlignment.pdf
```

---

## Sentence ID Format

Per Prof. Dien's specification — **18-character hierarchical ID**:

```
DSG_fff.ccc.ppp.ss
HVH_004.001.003.07
│││ ─── ─── ─── ──
│││  │   │   │   └─ ss:  sentence/box number (01–99)
│││  │   │   └───── ppp: page number within chapter (001–999)
│││  │   └───────── ccc: chapter number (001–999)
│││  └───────────── 004: file index in sub-domain
││└──────────────── H:   Genre (Hán script, vertical, carved-print)
│└───────────────── V:   Sub-domain (Vietnamese Historical)
└────────────────── H:   Domain (History)
```

---

## Algorithm: S1 ∩ S2 Character Alignment

For each OCR character `sn` paired with Quoc Ngu word `qn`:

| Condition | Status | Colour |
|-----------|--------|--------|
| `sn ∈ S2(qn)` | Correct | ⚫ BLACK |
| `S1(sn) ∩ S2(qn) ≥ 1` | Corrected | 🟢 GREEN |
| `S1(sn) ∩ S2(qn) = ∅` | OCR Failure | 🔴 RED |

Where:
- **S1** = `{ chars visually similar to sn }` — from `SinoNom_Similar.dic`
- **S2** = `{ chars valid translations of qn }` — from `QuocNgu_SinoNom.dic`

---

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Download Images

```bash
python data_scraper.py \
  --url "https://lib.nomfoundation.org/collection/1/volume/664/" \
  --out ./data/raw_images \
  --workers 6 \
  --delay 1.0
```

### 3. Test Layout Engine

```bash
python spatial_layout_engine.py
```

### 4. Test Alignment Validator

```bash
python alignment_validator.py
```

### 5. Run Full Pipeline

Open `hvm_dataset_generator.ipynb` in Jupyter or Google Colab and run all cells.

---

## Output Files

| File | Description |
|------|-------------|
| `output/xml/HVH_004_corpus.xml` | XML with full metadata header, chapter/page/sentence hierarchy |
| `output/excel/HVH_004_alignment_map.xlsx` | 3-sheet Excel: summary, char alignment, dictionary reference |

---

## Requirements

- Python 3.10+
- See `requirements.txt`

---

## Reference

- Prof. Dien, HCMUS — *SinoNom_OCR_TransliterationAlignment.pdf*
- Prof. Dien, HCMUS — *MidTerm_Requirement.pdf*
- [Nom Foundation Digital Library](https://lib.nomfoundation.org)
