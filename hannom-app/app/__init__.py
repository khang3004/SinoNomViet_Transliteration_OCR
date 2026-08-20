"""Image Prep — a stage in the crawl pipeline.

    crawl Facebook -> MinIO -> [this app: download images, sign URLs]
                            -> Gemini batch (boxing + OCR)

It downloads each crawled image and makes it fetchable from our own domain. It
does not read images — the OCR happens downstream.

``app.core`` holds the pipeline and must stay free of web-framework imports;
``app.api`` is the HTTP adapter; ``app.cli`` drives the same core headlessly.
"""
