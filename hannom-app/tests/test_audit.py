import random

import pytest

from app.core.audit import AuditError, AuditStore
from app.core.corpus import IngestReport
from app.core.models import Band, CorpusRecord, GeminiVerdict, Verdict


def record(post: str, idx: int, band: Band, ground_truth="年歲漸長", gemini="年歲漸長"):
    return CorpusRecord(
        record_id=f"{post}:{idx}",
        post_id=post,
        idx=idx,
        image=f"{post}_{idx}.jpg",
        ground_truth=ground_truth,
        gemini=gemini,
        band=band,
    )


@pytest.fixture
def store(tmp_path):
    audit = AuditStore(tmp_path)
    records = []
    n = 0
    for band in Band:
        for _ in range(40):
            records.append(record(f"post{n % 100}", n // 100, band))
            n += 1
    audit.corpus.replace(records, IngestReport(records=len(records)))
    return audit


class TestCorpusStore:
    def test_survives_a_reload(self, tmp_path):
        first = AuditStore(tmp_path)
        first.corpus.replace([record("p", 0, Band.EXACT)], IngestReport(records=1))
        assert len(AuditStore(tmp_path).corpus.records()) == 1

    def test_reingest_replaces_rather_than_appends(self, store):
        store.corpus.replace([record("p", 0, Band.EXACT)], IngestReport(records=1))
        assert len(store.corpus.records()) == 1

    def test_an_absent_corpus_is_empty_not_an_error(self, tmp_path):
        assert AuditStore(tmp_path).corpus.records() == []


class TestAssignment:
    def test_assigns_the_requested_count(self, store):
        result = store.assign("alice", 20, rng=random.Random(1))
        assert len(result.records) == 20
        assert len(store.queue("alice")) == 20

    def test_reviewers_never_share_a_record(self, store):
        a = store.assign("alice", 50, rng=random.Random(1))
        b = store.assign("bob", 50, rng=random.Random(1))
        assert not {r.record_id for r in a.records} & {r.record_id for r in b.records}

    def test_a_reviewer_sees_only_their_own_queue(self, store):
        store.assign("alice", 10, rng=random.Random(1))
        store.assign("bob", 10, rng=random.Random(2))
        assert {i.assignment.username for i in store.queue("alice")} == {"alice"}
        assert len(store.queue("bob")) == 10

    def test_topping_up_adds_to_the_existing_queue(self, store):
        store.assign("alice", 10, rng=random.Random(1))
        store.assign("alice", 5, rng=random.Random(2))
        assert len(store.queue("alice")) == 15

    def test_assignments_survive_a_reload(self, store, tmp_path):
        store.assign("alice", 10, rng=random.Random(1))
        assert len(AuditStore(tmp_path).queue("alice")) == 10

    def test_asking_for_nothing_is_rejected(self, store):
        with pytest.raises(AuditError):
            store.assign("alice", 0)

    def test_released_records_return_to_the_pool(self, store):
        result = store.assign("alice", 5, rng=random.Random(1))
        target = result.records[0].record_id
        store.release(target, "alice")
        assert target not in {i.record.record_id for i in store.queue("alice")}
        assert target in {r.record_id for r in store.available()}


class TestSubmit:
    def _assign_one(self, store, user="alice"):
        return store.assign(user, 1, rng=random.Random(1)).records[0]

    def test_correct_adopts_their_transcription_as_the_truth(self, store):
        item = self._assign_one(store)
        review = store.submit("alice", item.record_id, Verdict.CORRECT)
        # Never prefilled in the UI, filled here — so "clicked through without
        # looking" cannot masquerade as a perfect score.
        assert review.corrected == item.ground_truth
        assert review.ground_truth_similarity["cer_accuracy"] == 1.0

    def test_wrong_requires_the_corrected_text(self, store):
        item = self._assign_one(store)
        with pytest.raises(AuditError, match="correct transcription"):
            store.submit("alice", item.record_id, Verdict.WRONG)

    def test_a_correction_is_scored_against_their_label(self, store):
        item = self._assign_one(store)
        review = store.submit(
            "alice", item.record_id, Verdict.MINOR, corrected="年歲漸增"
        )
        assert review.ground_truth_similarity["distance"] == 1
        assert review.ground_truth_similarity["cer_accuracy"] == 0.75

    def test_gemini_is_scored_against_the_audited_truth_not_their_label(self, store):
        store.corpus.replace(
            [record("p", 0, Band.FAR, ground_truth="錯誤文字", gemini="正確文字")],
            IngestReport(records=1),
        )
        store.assign("alice", 1, rng=random.Random(1))
        review = store.submit("alice", "p:0", Verdict.WRONG, corrected="正確文字")
        assert review.gemini_vs_corrected["cer_accuracy"] == 1.0
        assert review.ground_truth_similarity["cer_accuracy"] < 1.0

    def test_unreadable_records_no_transcription(self, store):
        item = self._assign_one(store)
        review = store.submit("alice", item.record_id, Verdict.UNREADABLE)
        assert review.corrected == ""
        assert review.ground_truth_similarity == {}

    def test_a_broken_image_is_handed_back_for_replacement(self, store):
        item = self._assign_one(store)
        store.submit("alice", item.record_id, Verdict.NOT_AN_IMAGE)
        assert store.queue("alice") == []
        assert item.record_id in {r.record_id for r in store.available()}

    def test_a_broken_image_does_not_count_as_reviewed(self, store):
        item = self._assign_one(store)
        store.submit("alice", item.record_id, Verdict.NOT_AN_IMAGE)
        assert store.progress()["reviewed"] == 0

    def test_reviewers_cannot_touch_each_others_records(self, store):
        item = self._assign_one(store, "alice")
        with pytest.raises(AuditError, match="assigned to alice"):
            store.submit("bob", item.record_id, Verdict.CORRECT)

    def test_an_admin_can_override(self, store):
        item = self._assign_one(store, "alice")
        review = store.submit(
            "root", item.record_id, Verdict.CORRECT, is_admin=True
        )
        assert review.username == "root"

    def test_unknown_records_are_rejected(self, store):
        with pytest.raises(AuditError, match="Unknown record"):
            store.submit("alice", "nope:0", Verdict.CORRECT)

    def test_changing_a_verdict_keeps_the_latest(self, store):
        item = self._assign_one(store)
        store.submit("alice", item.record_id, Verdict.CORRECT)
        store.submit("alice", item.record_id, Verdict.WRONG, corrected="改")
        state = store.review_state()[item.record_id]
        assert state.verdict is Verdict.WRONG
        # The earlier verdict is still on disk — nothing is destroyed.
        assert len(store.reviews.rows()) == 2


class TestProgress:
    def test_counts_assigned_reviewed_and_pending(self, store):
        result = store.assign("alice", 10, rng=random.Random(1))
        for item in result.records[:4]:
            store.submit("alice", item.record_id, Verdict.CORRECT)

        progress = store.progress()
        assert progress["assigned"] == 10
        assert progress["reviewed"] == 4
        assert progress["pending"] == 6
        assert progress["percent"] == 40.0

    def test_reports_fill_per_band(self, store):
        store.assign("alice", 100, rng=random.Random(1))
        bands = {b["band"]: b for b in store.progress()["bands"]}
        assert bands["exact"]["assigned"] == 15
        assert bands["poor"]["assigned"] == 25
        assert bands["exact"]["in_corpus"] == 40
        assert bands["exact"]["available"] == 25

    def test_reports_the_headline_accuracies(self, store):
        store.corpus.replace(
            [record("p", 0, Band.FAR, ground_truth="錯誤文字", gemini="正確文字")],
            IngestReport(records=1),
        )
        store.assign("alice", 1, rng=random.Random(1))
        store.submit("alice", "p:0", Verdict.WRONG, corrected="正確文字")

        progress = store.progress()
        # Gemini turned out to be right and their label wrong in two of four
        # characters — the inversion this whole audit exists to detect.
        assert progress["gemini_accuracy"] == 1.0
        assert progress["ground_truth_accuracy"] == 0.5
        assert progress["audited_pairs"] == 1

    def test_accuracies_are_none_before_any_review(self, store):
        assert store.progress()["ground_truth_accuracy"] is None


class TestTeamView:
    def test_per_reviewer_rows(self, store):
        a = store.assign("alice", 10, rng=random.Random(1))
        store.assign("bob", 6, rng=random.Random(2))
        for item in a.records[:3]:
            store.submit("alice", item.record_id, Verdict.CORRECT)

        rows = {r["username"]: r for r in store.per_reviewer()}
        assert rows["alice"]["assigned"] == 10
        assert rows["alice"]["reviewed"] == 3
        assert rows["alice"]["remaining"] == 7
        assert rows["bob"]["reviewed"] == 0

    def test_flagged_images_are_counted_separately(self, store):
        result = store.assign("alice", 5, rng=random.Random(1))
        store.submit("alice", result.records[0].record_id, Verdict.NOT_AN_IMAGE)
        row = {r["username"]: r for r in store.per_reviewer()}["alice"]
        assert row["flagged_unusable"] == 1
        assert row["reviewed"] == 0

    def test_everyone_can_see_everyone_elses_completed_work(self, store):
        a = store.assign("alice", 3, rng=random.Random(1))
        b = store.assign("bob", 3, rng=random.Random(2))
        store.submit("alice", a.records[0].record_id, Verdict.CORRECT)
        store.submit("bob", b.records[0].record_id, Verdict.CORRECT)

        assert len(store.all_reviewed()) == 2
        assert len(store.all_reviewed(["bob"])) == 1


class TestExport:
    def test_one_row_per_reviewed_record(self, store):
        result = store.assign("alice", 3, rng=random.Random(1))
        store.submit(
            "alice", result.records[0].record_id, Verdict.MINOR,
            corrected="年歲漸增", note="variant form",
            gemini_verdict=GeminiVerdict.PARTIAL,
        )
        rows = store.export_rows()
        assert len(rows) == 1
        row = rows[0]
        assert row["verdict"] == "minor"
        assert row["corrected"] == "年歲漸增"
        assert row["reviewer"] == "alice"
        assert row["gemini_verdict"] == "partial"
        # Numeric, not "97.50%" — a text column cannot be averaged.
        assert isinstance(row["ground_truth_cer_accuracy"], float)
