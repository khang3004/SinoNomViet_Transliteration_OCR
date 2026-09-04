import random
from collections import Counter

import pytest

from app.core.audit import AuditError, AuditStore
from app.core.corpus import IngestReport
from app.core.models import Band, CorpusRecord, GeminiVerdict, Verdict

SAMPLE_SIZE = 100


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


def corpus_records(per_band: int = 40, posts: int = 100) -> list[CorpusRecord]:
    """A corpus far larger than the study, spread across many posts."""
    records = []
    n = 0
    for band in Band:
        for _ in range(per_band):
            records.append(record(f"post{n % posts}", n // posts, band))
            n += 1
    return records


@pytest.fixture
def loaded(tmp_path):
    """Corpus loaded, no study drawn yet."""
    audit = AuditStore(tmp_path)
    records = corpus_records()
    audit.corpus.replace(records, IngestReport(records=len(records)))
    return audit


@pytest.fixture
def store(loaded):
    """Corpus loaded and a study of 100 drawn."""
    loaded.create_sample(SAMPLE_SIZE, rng=random.Random(42))
    return loaded


class TestCorpusStore:
    def test_survives_a_reload(self, tmp_path):
        first = AuditStore(tmp_path)
        first.corpus.replace([record("p", 0, Band.EXACT)], IngestReport(records=1))
        assert len(AuditStore(tmp_path).corpus.records()) == 1

    def test_reingest_replaces_rather_than_appends(self, loaded):
        loaded.corpus.replace([record("p", 0, Band.EXACT)], IngestReport(records=1))
        assert len(loaded.corpus.records()) == 1

    def test_an_absent_corpus_is_empty_not_an_error(self, tmp_path):
        assert AuditStore(tmp_path).corpus.records() == []


class TestStudySample:
    def test_draws_exactly_the_requested_size(self, loaded):
        result = loaded.create_sample(SAMPLE_SIZE, rng=random.Random(1))
        assert len(result.added) == SAMPLE_SIZE
        assert len(loaded.sample.members()) == SAMPLE_SIZE

    def test_the_distribution_applies_to_the_sample_not_to_each_batch(self, store):
        """500-of-9000 is the point: the shares describe the study itself."""
        counts = Counter(r.band for r in store.sample_records())
        assert counts[Band.EXACT] == 15
        assert counts[Band.NEAR] == 20
        assert counts[Band.FAR] == 25
        assert counts[Band.POOR] == 25
        assert counts[Band.EMPTY] == 15

    def test_no_post_exceeds_the_cap_within_the_study(self, loaded):
        loaded.create_sample(SAMPLE_SIZE, per_post_cap=1, rng=random.Random(3))
        counts = Counter(r.post_id for r in loaded.sample_records())
        assert max(counts.values()) == 1

    def test_the_study_is_a_small_slice_of_the_corpus(self, store):
        assert len(store.corpus.records()) == 200
        assert len(store.sample_records()) == SAMPLE_SIZE

    def test_survives_a_reload(self, store, tmp_path):
        assert len(AuditStore(tmp_path).sample.members()) == SAMPLE_SIZE

    def test_a_thin_corpus_yields_a_short_study_and_says_so(self, tmp_path):
        audit = AuditStore(tmp_path)
        audit.corpus.replace(
            [record(f"p{i}", 0, Band.POOR) for i in range(10)],
            IngestReport(records=10),
        )
        result = audit.create_sample(SAMPLE_SIZE, rng=random.Random(1))
        assert len(result.added) == 10 and result.short == 90

    def test_cannot_draw_without_a_corpus(self, tmp_path):
        with pytest.raises(AuditError, match="Load the upstream data"):
            AuditStore(tmp_path).create_sample(SAMPLE_SIZE)

    def test_redrawing_replaces_membership(self, store):
        first = set(store.sample.members())
        store.create_sample(SAMPLE_SIZE, rng=random.Random(999))
        second = set(store.sample.members())
        assert len(second) == SAMPLE_SIZE
        assert first != second

    def test_redrawing_keeps_reviews_already_recorded(self, store):
        item = store.assign("alice", 1, rng=random.Random(1)).records[0]
        store.submit("alice", item.record_id, Verdict.CORRECT)
        store.create_sample(SAMPLE_SIZE, rng=random.Random(7))
        assert item.record_id in store.review_state()


class TestAssignment:
    def test_assigns_the_requested_count(self, store):
        result = store.assign("alice", 20, rng=random.Random(1))
        assert len(result.records) == 20
        assert len(store.queue("alice")) == 20

    def test_only_ever_hands_out_study_records(self, store):
        """The other 100 corpus images must never reach a reviewer."""
        members = store.sample.members()
        result = store.assign("alice", 40, rng=random.Random(1))
        assert {r.record_id for r in result.records} <= members

    def test_refuses_before_a_study_exists(self, loaded):
        with pytest.raises(AuditError, match="No study sample"):
            loaded.assign("alice", 10)

    def test_reviewers_never_share_a_record(self, store):
        a = store.assign("alice", 40, rng=random.Random(1))
        b = store.assign("bob", 40, rng=random.Random(1))
        assert not {r.record_id for r in a.records} & {r.record_id for r in b.records}

    def test_the_study_runs_out_rather_than_spilling_into_the_corpus(self, store):
        result = store.assign("alice", SAMPLE_SIZE + 50, rng=random.Random(1))
        assert len(result.records) == SAMPLE_SIZE
        assert result.short == 50

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

    def test_released_records_return_to_the_study_pool(self, store):
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
        # Filled server-side when the client sends nothing, so the verdict is
        # always what decides — see TestPreSeededCorrection for the guard that
        # keeps the pre-filled box from becoming a rubber stamp.
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

    def test_gemini_is_scored_against_the_audited_truth_not_their_label(self, tmp_path):
        store = AuditStore(tmp_path)
        store.corpus.replace(
            [record("p", 0, Band.FAR, ground_truth="錯誤文字", gemini="正確文字")],
            IngestReport(records=1),
        )
        store.create_sample(1, rng=random.Random(1))
        store.assign("alice", 1, rng=random.Random(1))
        review = store.submit("alice", "p:0", Verdict.WRONG, corrected="正確文字")
        assert review.gemini_vs_corrected["cer_accuracy"] == 1.0
        assert review.ground_truth_similarity["cer_accuracy"] == 0.5

    def test_unreadable_records_no_transcription(self, store):
        item = self._assign_one(store)
        review = store.submit("alice", item.record_id, Verdict.UNREADABLE)
        assert review.corrected == ""
        assert review.ground_truth_similarity == {}

    def test_reviewers_cannot_touch_each_others_records(self, store):
        item = self._assign_one(store, "alice")
        with pytest.raises(AuditError, match="assigned to alice"):
            store.submit("bob", item.record_id, Verdict.CORRECT)

    def test_an_admin_can_override(self, store):
        item = self._assign_one(store, "alice")
        review = store.submit("root", item.record_id, Verdict.CORRECT, is_admin=True)
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


class TestUnusableImages:
    def _assign_one(self, store, user="alice"):
        return store.assign(user, 1, rng=random.Random(1)).records[0]

    def test_a_broken_image_leaves_the_queue_and_a_replacement_takes_its_place(
        self, store
    ):
        """The reviewer asked for one image to review and should still have one."""
        item = self._assign_one(store)
        store.submit("alice", item.record_id, Verdict.NOT_AN_IMAGE)
        queue = store.queue("alice")
        assert [i.record.record_id for i in queue] != [item.record_id]
        assert len(queue) == 1

    def test_it_does_not_count_as_reviewed(self, store):
        item = self._assign_one(store)
        store.submit("alice", item.record_id, Verdict.NOT_AN_IMAGE)
        assert store.progress()["reviewed"] == 0

    def test_the_study_is_refilled_to_its_target_size(self, store):
        """500 judged images was the plan, not 500 minus the broken ones."""
        item = self._assign_one(store)
        store.submit("alice", item.record_id, Verdict.NOT_AN_IMAGE)
        assert len(store.sample.members()) == SAMPLE_SIZE

    def test_the_replacement_comes_from_the_same_band(self, store):
        item = self._assign_one(store)
        before = Counter(r.band for r in store.sample_records())
        store.submit("alice", item.record_id, Verdict.NOT_AN_IMAGE)
        assert Counter(r.band for r in store.sample_records()) == before

    def test_a_dropped_image_is_never_handed_out_again(self, store):
        item = self._assign_one(store)
        store.submit("alice", item.record_id, Verdict.NOT_AN_IMAGE)
        assert item.record_id not in store.sample.members()
        assert item.record_id not in {r.record_id for r in store.available()}
        store.assign("bob", SAMPLE_SIZE, rng=random.Random(5))
        assert item.record_id not in {i.record.record_id for i in store.queue("bob")}

    def test_the_drop_is_recorded_with_its_reason(self, store):
        item = self._assign_one(store)
        store.submit("alice", item.record_id, Verdict.NOT_AN_IMAGE)
        assert store.sample.dropped()[item.record_id] == "flagged_unusable"

    def test_top_up_is_a_no_op_when_the_study_is_full(self, store):
        assert store.top_up_sample().added == []

    def test_an_exhausted_corpus_cannot_refill_forever(self, tmp_path):
        audit = AuditStore(tmp_path)
        audit.corpus.replace(
            [record(f"p{i}", 0, Band.POOR) for i in range(3)], IngestReport(records=3)
        )
        audit.create_sample(3, rng=random.Random(1))
        for item in audit.assign("alice", 3, rng=random.Random(1)).records:
            audit.submit("alice", item.record_id, Verdict.NOT_AN_IMAGE)
        # Nothing left to draw, and no crash — the study is simply short.
        assert audit.sample.members() == set()
        assert audit.progress()["percent"] == 0.0


class TestProgress:
    def test_the_denominator_is_the_study_not_what_was_claimed(self, store):
        result = store.assign("alice", 10, rng=random.Random(1))
        for item in result.records[:4]:
            store.submit("alice", item.record_id, Verdict.CORRECT)

        progress = store.progress()
        assert progress["target"] == SAMPLE_SIZE
        assert progress["assigned"] == 10
        assert progress["reviewed"] == 4
        assert progress["pending"] == 6
        assert progress["unclaimed"] == 90
        # 4 of the study's 100 — not 40% of the 10 someone happened to claim.
        assert progress["percent"] == 4.0

    def test_reports_the_share_of_the_corpus_sampled(self, store):
        assert store.progress()["sampled_percent"] == 50.0

    def test_reports_target_and_fill_per_band(self, store):
        store.assign("alice", SAMPLE_SIZE, rng=random.Random(1))
        bands = {b["band"]: b for b in store.progress()["bands"]}
        assert bands["exact"]["target"] == 15
        assert bands["exact"]["in_sample"] == 15
        assert bands["exact"]["assigned"] == 15
        assert bands["exact"]["in_corpus"] == 40
        assert bands["poor"]["target"] == 25
        assert bands["poor"]["in_sample"] == 25

    def test_band_percent_tracks_reviews(self, store):
        exact = [r for r in store.sample_records() if r.band is Band.EXACT]
        store.assign("alice", SAMPLE_SIZE, rng=random.Random(1))
        for rec in exact[:3]:
            store.submit("alice", rec.record_id, Verdict.CORRECT)
        bands = {b["band"]: b for b in store.progress()["bands"]}
        assert bands["exact"]["reviewed"] == 3
        assert bands["exact"]["percent"] == 20.0

    def test_reports_the_headline_accuracies(self, tmp_path):
        store = AuditStore(tmp_path)
        store.corpus.replace(
            [record("p", 0, Band.FAR, ground_truth="錯誤文字", gemini="正確文字")],
            IngestReport(records=1),
        )
        store.create_sample(1, rng=random.Random(1))
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

    def test_the_sample_status_is_reported(self, store):
        sample = store.progress()["sample"]
        assert sample["exists"] and sample["size"] == SAMPLE_SIZE
        assert sample["active"] == SAMPLE_SIZE and sample["complete"]

    def test_progress_is_zero_before_a_study_is_drawn(self, loaded):
        progress = loaded.progress()
        assert progress["target"] == 0 and progress["percent"] == 0.0
        assert progress["sample"]["exists"] is False


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


class TestPreSeededCorrection:
    """The review screen seeds the correction box with their transcription.

    That is a real convenience for CJK — editing two characters beats retyping
    twenty — but it makes one contradiction reachable, so the server closes it.
    """

    def _assign_one(self, store, user="alice"):
        return store.assign(user, 1, rng=random.Random(1)).records[0]

    def test_wrong_with_an_unedited_correction_is_refused(self, store):
        item = self._assign_one(store)
        with pytest.raises(AuditError, match="identical to their transcription"):
            store.submit(
                "alice", item.record_id, Verdict.WRONG, corrected=item.ground_truth
            )

    def test_minor_with_an_unedited_correction_is_refused(self, store):
        item = self._assign_one(store)
        with pytest.raises(AuditError, match="identical to their transcription"):
            store.submit(
                "alice", item.record_id, Verdict.MINOR, corrected=item.ground_truth
            )

    def test_whitespace_only_edits_do_not_count_as_a_correction(self, store):
        """Line breaks are layout, not content — they are not a fix."""
        item = self._assign_one(store)
        with pytest.raises(AuditError, match="identical"):
            store.submit(
                "alice", item.record_id, Verdict.WRONG,
                corrected="  " + item.ground_truth.replace("", " ").strip() + "\n",
            )

    def test_a_real_edit_is_accepted(self, store):
        item = self._assign_one(store)
        review = store.submit(
            "alice", item.record_id, Verdict.MINOR, corrected="年歲漸增"
        )
        assert review.corrected == "年歲漸增"

    def test_correct_still_accepts_the_unedited_text(self, store):
        """Marking a label correct is exactly the case where it should match."""
        item = self._assign_one(store)
        review = store.submit(
            "alice", item.record_id, Verdict.CORRECT, corrected=item.ground_truth
        )
        assert review.ground_truth_similarity["cer_accuracy"] == 1.0


class TestSwapOut:
    """Reviewers can decline an image; the study keeps its size and its shape."""

    def _assign_one(self, store, user="alice"):
        return store.assign(user, 1, rng=random.Random(1)).records[0]

    def test_the_study_keeps_its_target_size(self, store):
        item = self._assign_one(store)
        store.swap_out(item.record_id, "alice", reason="not_wanted")
        assert len(store.sample.members()) == SAMPLE_SIZE

    def test_the_replacement_comes_from_the_same_band(self, store):
        item = self._assign_one(store)
        before = Counter(r.band for r in store.sample_records())
        store.swap_out(item.record_id, "alice", reason="not_wanted")
        assert Counter(r.band for r in store.sample_records()) == before

    def test_the_reviewer_is_handed_the_replacement(self, store):
        item = self._assign_one(store)
        replacement = store.swap_out(item.record_id, "alice", reason="not_wanted")
        assert replacement is not None
        assert replacement.record_id != item.record_id
        # Their queue is the size they asked for, not one short.
        assert len(store.queue("alice")) == 1
        assert store.queue("alice")[0].record.record_id == replacement.record_id

    def test_the_skipped_image_never_comes_back(self, store):
        item = self._assign_one(store)
        store.swap_out(item.record_id, "alice", reason="not_wanted")
        assert item.record_id not in store.sample.members()
        store.assign("bob", SAMPLE_SIZE, rng=random.Random(4))
        assert item.record_id not in {i.record.record_id for i in store.queue("bob")}

    def test_the_skip_is_recorded_against_the_reviewer(self, store):
        item = self._assign_one(store)
        store.swap_out(item.record_id, "alice", reason="not_wanted")
        row = {r["record_id"]: r for r in store.sample.drops()}[item.record_id]
        assert row["reason"] == "not_wanted"
        assert row["username"] == "alice"

    def test_skips_are_counted_per_reviewer(self, store):
        for item in store.assign("alice", 3, rng=random.Random(1)).records:
            store.swap_out(item.record_id, "alice", reason="not_wanted")
        row = {r["username"]: r for r in store.per_reviewer()}["alice"]
        assert row["skipped"] == 3
        assert row["reviewed"] == 0

    def test_skipping_does_not_count_as_progress(self, store):
        item = self._assign_one(store)
        store.swap_out(item.record_id, "alice", reason="not_wanted")
        progress = store.progress()
        assert progress["reviewed"] == 0
        assert progress["target"] == SAMPLE_SIZE

    def test_reasons_are_reported_separately(self, store):
        first = self._assign_one(store)
        store.swap_out(first.record_id, "alice", reason="not_wanted")
        second = store.queue("alice")[0].record
        store.submit("alice", second.record_id, Verdict.NOT_AN_IMAGE)

        reasons = store.progress()["sample"]["dropped_by_reason"]
        assert reasons["not_wanted"] == 1
        assert reasons["flagged_unusable"] == 1

    def test_another_reviewers_image_cannot_be_skipped(self, store):
        item = self._assign_one(store, "alice")
        with pytest.raises(AuditError, match="assigned to alice"):
            store.swap_out(item.record_id, "bob", reason="not_wanted")

    def test_an_unassigned_image_cannot_be_skipped(self, store):
        record = store.sample_records()[0]
        with pytest.raises(AuditError, match="not currently assigned"):
            store.swap_out(record.record_id, "alice", reason="not_wanted")

    def test_an_exhausted_pool_returns_no_replacement_rather_than_failing(
        self, tmp_path
    ):
        audit = AuditStore(tmp_path)
        audit.corpus.replace(
            [record(f"p{i}", 0, Band.POOR) for i in range(2)], IngestReport(records=2)
        )
        audit.create_sample(2, rng=random.Random(1))
        held = audit.assign("alice", 2, rng=random.Random(1)).records
        # Both are held, so nothing is left to swap in.
        assert audit.swap_out(held[0].record_id, "alice", reason="not_wanted") is None

    def test_the_reviewer_receives_the_same_band_replacement(self, store):
        """Their own mix of easy and hard images has to stay comparable too."""
        item = self._assign_one(store)
        replacement = store.swap_out(item.record_id, "alice", reason="not_wanted")
        assert replacement.band is item.band


class TestExtendStudy:
    """Growing a study must cost nothing that is already in it."""

    def test_the_target_grows(self, store):
        store.extend_sample(50, rng=random.Random(2))
        assert store.sample.size == SAMPLE_SIZE + 50
        assert len(store.sample.members()) == SAMPLE_SIZE + 50

    def test_existing_members_all_survive(self, store):
        before = set(store.sample.members())
        store.extend_sample(50, rng=random.Random(2))
        assert before <= set(store.sample.members())

    def test_reviews_and_assignments_are_untouched(self, store):
        held = store.assign("alice", 10, rng=random.Random(1)).records
        store.submit("alice", held[0].record_id, Verdict.CORRECT)

        store.extend_sample(50, rng=random.Random(2))

        assert held[0].record_id in store.review_state()
        assert len(store.queue("alice")) == 10
        assert store.progress()["reviewed"] == 1

    def test_the_new_images_have_never_been_used(self, store):
        before = set(store.sample.members())
        result = store.extend_sample(50, rng=random.Random(2))
        assert not {r.record_id for r in result.added} & before

    def test_swapped_out_images_are_not_drawn_back_in(self, store):
        item = store.assign("alice", 1, rng=random.Random(1)).records[0]
        store.swap_out(item.record_id, "alice", reason="not_wanted")
        store.extend_sample(50, rng=random.Random(2))
        assert item.record_id not in store.sample.members()

    def test_the_enlarged_study_keeps_its_band_shape(self, store):
        """The new images follow the plan for the NEW total, not the old one."""
        from app.core import sampling

        store.extend_sample(50, rng=random.Random(3))
        counts = Counter(r.band for r in store.sample_records())
        plan = sampling.quotas(SAMPLE_SIZE + 50, sampling.DEFAULT_TARGETS)
        assert {b: counts[b] for b in Band} == plan

    def test_shape_gives_way_to_size_when_the_corpus_runs_dry(self, store):
        """Extending to the whole 200-record corpus cannot hold the plan.

        Bands that run out have their quota redistributed, so the study fills to
        the requested size with a skewed shape rather than staying small. Worth
        knowing before extending close to the size of the corpus.
        """
        store.extend_sample(100, rng=random.Random(3))
        counts = Counter(r.band for r in store.sample_records())
        assert sum(counts.values()) == 200
        # far/poor wanted 50 each but only 40 of each exist.
        assert counts[Band.FAR] == 40 and counts[Band.POOR] == 40

    def test_progress_re_bases_on_the_larger_total(self, store):
        held = store.assign("alice", 4, rng=random.Random(1)).records
        for item in held:
            store.submit("alice", item.record_id, Verdict.CORRECT)
        assert store.progress()["percent"] == 4.0  # 4 of 100

        store.extend_sample(100, rng=random.Random(2))
        progress = store.progress()
        assert progress["target"] == 200
        assert progress["reviewed"] == 4
        assert progress["percent"] == 2.0  # the same 4, now of 200

    def test_extending_without_a_study_is_refused(self, loaded):
        with pytest.raises(AuditError, match="Draw a study sample before"):
            loaded.extend_sample(50)

    def test_a_short_corpus_extends_as_far_as_it_can(self, store):
        # The corpus holds 200; the study already has 100.
        result = store.extend_sample(500, rng=random.Random(4))
        assert len(result.added) == 100 and result.short == 400


class TestReassign:
    def test_moves_unreviewed_claims(self, store):
        held = store.assign("alice", 10, rng=random.Random(1)).records
        moved = store.reassign("alice", "bob")
        assert moved == 10
        assert store.queue("alice") == []
        assert len(store.queue("bob")) == 10
        assert {i.record.record_id for i in store.queue("bob")} == {
            r.record_id for r in held
        }

    def test_reviewed_work_stays_with_whoever_did_it(self, store):
        held = store.assign("alice", 10, rng=random.Random(1)).records
        store.submit("alice", held[0].record_id, Verdict.CORRECT)

        moved = store.reassign("alice", "bob")

        assert moved == 9
        # The finished one stays on alice's queue and in her totals.
        assert [i.record.record_id for i in store.queue("alice")] == [held[0].record_id]
        rows = {r["username"]: r for r in store.per_reviewer()}
        assert rows["alice"]["reviewed"] == 1
        assert rows["bob"]["assigned"] == 9

    def test_a_limit_moves_only_part_of_the_queue(self, store):
        store.assign("alice", 10, rng=random.Random(1))
        assert store.reassign("alice", "bob", limit=4) == 4
        assert len(store.queue("alice")) == 6
        assert len(store.queue("bob")) == 4

    def test_moving_to_yourself_is_rejected(self, store):
        store.assign("alice", 5, rng=random.Random(1))
        with pytest.raises(AuditError, match="two different reviewers"):
            store.reassign("alice", "alice")

    def test_an_empty_queue_moves_nothing(self, store):
        assert store.reassign("alice", "bob") == 0

    def test_the_study_is_unaffected(self, store):
        store.assign("alice", 10, rng=random.Random(1))
        before = set(store.sample.members())
        store.reassign("alice", "bob")
        assert set(store.sample.members()) == before
        assert store.progress()["assigned"] == 10
