"""JobStore tests — kind/payload (extract vs reocr) and orphan requeue."""

from __future__ import annotations

import json

from pipeline.jobstore import JobStore


def test_extract_and_reocr_jobs(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    ext = store.create("doc.pdf", "in.pdf", source_doc="ChauBan")
    payload = json.dumps({"image_path": "p0001.png", "bbox": [10, 20, 110, 60]})
    rec = store.create("reocr p1", "", kind="reocr", payload=payload)

    j1 = store.claim_next()  # oldest first → the extract job
    assert j1.id == ext and j1.kind == "extract"
    j2 = store.claim_next()  # then the reocr job
    assert j2.id == rec and j2.kind == "reocr"
    assert json.loads(j2.payload)["bbox"] == [10, 20, 110, 60]
    assert store.claim_next() is None


def test_requeue_running(tmp_path):
    store = JobStore(str(tmp_path / "jobs.db"))
    store.create("a.pdf", "a.pdf")
    store.claim_next()  # now RUNNING
    assert store.requeue_running() == 1
    again = store.claim_next()  # claimable once more
    assert again is not None and again.status == "running"
