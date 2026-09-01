import random
from collections import Counter

from app.core import sampling
from app.core.models import Band, CorpusRecord


def record(post: str, idx: int, band: Band) -> CorpusRecord:
    return CorpusRecord(
        record_id=f"{post}:{idx}",
        post_id=post,
        idx=idx,
        image=f"{post}_{idx}.jpg",
        ground_truth="年歲漸長",
        band=band,
    )


def pool(per_band: int = 200, posts: int = 400) -> list[CorpusRecord]:
    """A deep pool: every band well stocked, images spread across posts."""
    records = []
    n = 0
    for band in Band:
        for _ in range(per_band):
            records.append(record(f"post{n % posts}", n // posts, band))
            n += 1
    return records


class TestQuotas:
    def test_always_sums_to_exactly_n(self):
        for n in range(0, 200):
            assert sum(sampling.quotas(n, sampling.DEFAULT_TARGETS).values()) == n

    def test_follows_the_configured_shares(self):
        got = sampling.quotas(100, sampling.DEFAULT_TARGETS)
        assert got[Band.EXACT] == 15
        assert got[Band.NEAR] == 20
        assert got[Band.FAR] == 25
        assert got[Band.POOR] == 25
        assert got[Band.EMPTY] == 15

    def test_unnormalized_weights_are_accepted(self):
        got = sampling.quotas(100, {Band.EXACT: 1, Band.POOR: 3})
        assert got == {Band.EXACT: 25, Band.POOR: 75}

    def test_zero_and_negative_requests_are_empty(self):
        assert sum(sampling.quotas(0, sampling.DEFAULT_TARGETS).values()) == 0
        assert sum(sampling.quotas(-5, sampling.DEFAULT_TARGETS).values()) == 0


class TestDraw:
    def test_draws_the_requested_count(self):
        result = sampling.draw(pool(), n=100, rng=random.Random(1))
        assert len(result.records) == 100
        assert result.short == 0

    def test_hits_the_target_distribution(self):
        result = sampling.draw(
            pool(), n=200, per_post_cap=0, rng=random.Random(7)
        )
        counts = Counter(r.band for r in result.records)
        assert counts[Band.EXACT] == 30
        assert counts[Band.NEAR] == 40
        assert counts[Band.FAR] == 50
        assert counts[Band.POOR] == 50
        assert counts[Band.EMPTY] == 30

    def test_records_are_never_repeated(self):
        result = sampling.draw(pool(), n=300, rng=random.Random(3))
        ids = [r.record_id for r in result.records]
        assert len(ids) == len(set(ids))

    def test_is_random_across_seeds(self):
        first = sampling.draw(pool(), n=50, rng=random.Random(1)).records
        second = sampling.draw(pool(), n=50, rng=random.Random(2)).records
        assert [r.record_id for r in first] != [r.record_id for r in second]

    def test_the_same_seed_reproduces_a_sample(self):
        first = sampling.draw(pool(), n=50, rng=random.Random(11)).records
        second = sampling.draw(pool(), n=50, rng=random.Random(11)).records
        assert [r.record_id for r in first] == [r.record_id for r in second]


class TestPerPostCap:
    def test_no_post_exceeds_the_cap(self):
        # One prolific post with 50 images, plus a thin spread of others.
        records = [record("prolific", i, Band.POOR) for i in range(50)]
        records += [record(f"other{i}", 0, Band.POOR) for i in range(50)]
        result = sampling.draw(
            records, n=60, targets={Band.POOR: 1.0}, per_post_cap=2,
            rng=random.Random(5),
        )
        counts = Counter(r.post_id for r in result.records)
        assert counts["prolific"] == 2
        assert max(counts.values()) <= 2

    def test_the_cap_counts_images_already_assigned_elsewhere(self):
        """No overlap between reviewers, so the cap is a global running total."""
        records = [record("prolific", i, Band.POOR) for i in range(50)]
        result = sampling.draw(
            records, n=10, targets={Band.POOR: 1.0}, per_post_cap=2,
            post_counts={"prolific": 2}, rng=random.Random(5),
        )
        assert result.records == []
        assert result.blocked_by_post_cap > 0

    def test_a_cap_of_zero_disables_it(self):
        records = [record("prolific", i, Band.POOR) for i in range(50)]
        result = sampling.draw(
            records, n=20, targets={Band.POOR: 1.0}, per_post_cap=0,
            rng=random.Random(5),
        )
        assert len(result.records) == 20


class TestShortfall:
    def test_a_thin_band_is_topped_up_from_the_others(self):
        records = [record(f"p{i}", 0, Band.EXACT) for i in range(3)]
        records += [record(f"q{i}", 0, Band.POOR) for i in range(200)]
        result = sampling.draw(records, n=100, per_post_cap=0, rng=random.Random(9))

        assert len(result.records) == 100
        counts = Counter(r.band for r in result.records)
        # Only three exact rows exist, so the other 12 of its quota moved on.
        assert counts[Band.EXACT] == 3
        assert result.shortfall[Band.EXACT] == 12

    def test_an_exhausted_pool_returns_what_it_has_rather_than_looping(self):
        records = [record(f"p{i}", 0, Band.POOR) for i in range(5)]
        result = sampling.draw(records, n=100, per_post_cap=0, rng=random.Random(2))
        assert len(result.records) == 5
        assert result.short == 95

    def test_an_empty_pool_is_not_an_error(self):
        result = sampling.draw([], n=10, rng=random.Random(1))
        assert result.records == [] and result.short == 10


class TestOrdering:
    def test_bands_are_interleaved_not_blocked(self):
        """Sixty identical-match rows in a row would skew a reviewer's attention."""
        result = sampling.draw(pool(), n=100, per_post_cap=0, rng=random.Random(4))
        bands = [r.band for r in result.records]
        runs = sum(1 for a, b in zip(bands, bands[1:]) if a is not b)
        assert runs > 50
