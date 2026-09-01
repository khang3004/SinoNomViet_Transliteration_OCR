"""OCR Review — auditing another team's Hán-Nôm transcriptions.

    their Drive folder + ground_truth files
        -> [this app: stratified sample, human verdicts]
        -> reviews.xlsx with audited accuracy

The question on every screen is whether their ``ground_truth`` matches the
image. Everything else — the sampling, the accounts, the exports — exists to
make that judgement cheap to make and hard to fake.

``app.core`` holds the logic and must stay free of web-framework imports;
``app.api`` is the HTTP adapter; ``app.cli`` drives the same core headlessly.
"""
