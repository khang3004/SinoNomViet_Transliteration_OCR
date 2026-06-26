"""hannom-app pipeline package.

Corpus-builder pipeline for Han-Nom OCR. Produces parallel JSONL
(Han · phonetic · Vietnamese meaning · metadata) for training MT models.

The two extensibility seams live here:
  - ``pipeline.ocr``     : OCR engine registry  (paddle / vision / kandianguji / mock)
  - ``pipeline.layouts`` : layout handler registry (two_column / three_block / han_only)

Adding a new OCR engine or document layout later = drop a new file into the
respective sub-package and make a single ``register(...)`` call. No existing
code is touched.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
