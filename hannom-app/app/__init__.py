"""Han Scanner — a stage in the crawl pipeline.

    crawl Facebook -> MinIO -> [this app: does the image contain Han text?]
                            -> Gemini batch (boxing + OCR)

``app.core`` holds the pipeline and must stay free of web-framework imports;
``app.api`` is the HTTP adapter; ``app.cli`` drives the same core headlessly.
"""
