# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Planned
- Integration with real OCR APIs (PaddleOCR, Google Vision)
- Full S1/S2 dictionaries loaded from Sino-Nom dictionaries
- Sentence-level box alignment (m-n mapping)
- NER tagging for PER, LOC, ORG, TITLE, TME, NUM
- GitHub Actions CI pipeline

---

## [0.1.0] — 2024-06-19

### Added
- `data_scraper.py` — async image downloader for Nom Foundation library  
  - IIIF manifest discovery + HTML fallback strategy  
  - Configurable concurrency, retry backoff, polite delay  
  - Clean `page_NNN.jpg` naming convention  
- `spatial_layout_engine.py` — Right-to-Left column clustering engine  
  - Adaptive Horizontal Threshold (AHT) algorithm  
  - `BoundingBox`, `Column` data structures with full type hints  
  - PaddleOCR result parser (`BoundingBox.from_paddleocr`)  
- `alignment_validator.py` — S1∩S2 Levenshtein alignment validator  
  - Alignment validation: BLACK / GREEN / RED status  
  - Full Needleman-Wunsch sequence alignment  
  - Dictionary loaders (`load_s1_from_file`, `load_s2_from_file`)  
- `hvm_dataset_generator.ipynb` — master Colab/Jupyter pipeline notebook  
  - Google Drive mount + path configuration  
  - `SentenceIDGenerator` producing `HVH_004.ccc.ppp.ss` (18-char IDs)  
  - XML export with metadata headers  
  - 3-sheet Excel export (summary, char alignment, dictionary reference)  
  - Validation assertions on all outputs  
- `pyproject.toml` — PEP 517/518 project with `uv` dev dependencies  
- `Makefile` — convenient `make install`, `make run`, `make test`, etc.  
- `data/dicts/SinoNom_Similar.dic` — S1 dictionary seed (~40 entries)  
- `data/dicts/QuocNgu_SinoNom.dic` — S2 dictionary seed (~40 entries)  
- Community files: `LICENSE`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`,
  `SECURITY.md`, `CHANGELOG.md`

[Unreleased]: https://github.com/khang3004/SinoNomViet_Transliteration_OCR/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/khang3004/SinoNomViet_Transliteration_OCR/releases/tag/v0.1.0
