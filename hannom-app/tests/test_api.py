"""End-to-end over HTTP: sign in, take work, judge it, export it."""

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.core.users import hash_password  # noqa: E402

ADMIN_PASSWORD = "admin-password"
POST_A = "UzpfSTEwMDAwMDU5MzExMzI1ODpWSzoyNzgzNTQ4OTgyNjA5MzEwMA=="


def image_name(i):
    return f"{POST_A[:-4]}{i:03d}==_0.jpg"


def seed_drive_index(tmp_path, count=40, indexed=None):
    """A Drive index, as a folder listing would have produced.

    The study is drawn only from images the index can resolve, so without this
    nothing is drawable — which is the behaviour, not a test fixture quirk.
    """
    names = range(count) if indexed is None else indexed
    (tmp_path / "drive_index.json").write_text(
        json.dumps(
            {
                "meta": {"method": "test", "images": len(list(names)), "truncated": False},
                "files": {
                    image_name(i): {"id": f"driveid{i:04d}" + "x" * 20,
                                    "mime": "image/jpeg", "size": 1000}
                    for i in (range(count) if indexed is None else indexed)
                },
            }
        ),
        encoding="utf-8",
    )


def make_jsonl(path, count=40):
    """A spread of bands so a stratified draw has something to draw from."""
    rows = []
    for i in range(count):
        gemini = {
            0: "年歲漸長",           # exact
            1: "年歲漸長心",         # near-ish
            2: "年歲億兆",           # diverging
            3: "",                   # empty
        }[i % 4]
        rows.append(
            {
                "image": image_name(i),
                "ground_truth": "年歲漸長",
                "label": "",
                "gemini": [{"text": gemini}],
                "deepseek": [{"text": "年歲"}],
            }
        )
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "t" * 48)
    monkeypatch.setenv("APP_USERNAME", "root")
    monkeypatch.setenv("APP_PASSWORD_HASH", hash_password(ADMIN_PASSWORD))
    monkeypatch.setenv("COOKIE_SECURE", "0")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IMAGE_SIGNING_SECRET", "s" * 48)
    monkeypatch.setenv("SAMPLE_SIZE", "24")
    monkeypatch.setenv("SAMPLE_BATCH", "20")

    from app.api.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def login(client, username, password):
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()


def seed_corpus(client, tmp_path, count=40, sample=True, indexed=None):
    """Sign in as admin, upload a ground_truth.jsonl, ingest it, draw the study."""
    login(client, "root", ADMIN_PASSWORD)
    seed_drive_index(tmp_path, count, indexed)
    path = tmp_path / "ground_truth.jsonl"
    make_jsonl(path, count)
    with open(path, "rb") as handle:
        response = client.post(
            "/api/corpus/upload",
            files={"file": ("ground_truth.jsonl", handle, "application/x-ndjson")},
        )
    assert response.status_code == 200, response.text
    response = client.post("/api/corpus/ingest")
    assert response.status_code == 200, response.text
    ingested = response.json()
    if sample:
        drawn = client.post("/api/sample", json={})
        assert drawn.status_code == 200, drawn.text
    return ingested


class TestAuth:
    def test_the_app_is_closed_without_a_session(self, client):
        assert client.get("/api/progress").status_code == 401
        assert client.get("/").status_code == 401

    def test_the_super_admin_can_sign_in(self, client):
        assert login(client, "root", ADMIN_PASSWORD)["role"] == "admin"

    def test_a_wrong_password_is_rejected(self, client):
        response = client.post(
            "/api/auth/login", json={"username": "root", "password": "nope"}
        )
        assert response.status_code == 401

    def test_repeated_failures_lock_the_ip_out(self, client):
        for _ in range(5):
            client.post(
                "/api/auth/login", json={"username": "root", "password": "nope"}
            )
        response = client.post(
            "/api/auth/login", json={"username": "root", "password": "nope"}
        )
        assert response.status_code == 429

    def test_signing_out_ends_the_session(self, client):
        login(client, "root", ADMIN_PASSWORD)
        assert client.get("/api/progress").status_code == 200
        client.post("/api/auth/logout")
        assert client.get("/api/progress").status_code == 401

    def test_healthz_needs_no_session(self, client):
        assert client.get("/healthz").status_code == 200


class TestUserManagement:
    def test_an_admin_can_add_a_reviewer_who_can_then_sign_in(self, client):
        login(client, "root", ADMIN_PASSWORD)
        response = client.post(
            "/api/users",
            json={"username": "mai", "password": "password123", "display_name": "Mai"},
        )
        assert response.status_code == 200, response.text

        client.post("/api/auth/logout")
        assert login(client, "mai", "password123")["role"] == "reviewer"

    def test_a_reviewer_cannot_add_users(self, client):
        login(client, "root", ADMIN_PASSWORD)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/auth/logout")
        login(client, "mai", "password123")

        response = client.post(
            "/api/users", json={"username": "bob", "password": "password123"}
        )
        assert response.status_code == 403

    def test_a_reviewer_can_see_the_roster(self, client):
        login(client, "root", ADMIN_PASSWORD)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/auth/logout")
        login(client, "mai", "password123")

        names = [u["username"] for u in client.get("/api/users").json()["users"]]
        assert "root" in names and "mai" in names

    def test_no_password_hash_is_ever_returned(self, client):
        login(client, "root", ADMIN_PASSWORD)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        assert "password_hash" not in client.get("/api/users").text

    def test_a_disabled_reviewer_cannot_sign_in(self, client):
        login(client, "root", ADMIN_PASSWORD)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/users/mai/active", json={"active": False})
        client.post("/api/auth/logout")

        response = client.post(
            "/api/auth/login", json={"username": "mai", "password": "password123"}
        )
        assert response.status_code == 401


class TestCorpus:
    def test_upload_and_ingest(self, client, tmp_path):
        result = seed_corpus(client, tmp_path)
        assert result["records"] == 40
        assert client.get("/api/corpus").json()["records"] == 40

    def test_ingest_without_files_explains_itself(self, client):
        login(client, "root", ADMIN_PASSWORD)
        response = client.post("/api/corpus/ingest")
        assert response.status_code == 400
        assert "Upload" in response.json()["detail"]

    def test_a_wrong_file_type_is_rejected(self, client, tmp_path):
        login(client, "root", ADMIN_PASSWORD)
        path = tmp_path / "notes.txt"
        path.write_text("hello", encoding="utf-8")
        with open(path, "rb") as handle:
            response = client.post(
                "/api/corpus/upload",
                files={"file": ("notes.txt", handle, "text/plain")},
            )
        assert response.status_code == 400

    def test_a_reviewer_cannot_replace_the_corpus(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/auth/logout")
        login(client, "mai", "password123")
        assert client.post("/api/corpus/ingest").status_code == 403


class TestReviewFlow:
    def test_the_full_loop(self, client, tmp_path):
        seed_corpus(client, tmp_path)

        assigned = client.post("/api/queue/assign", json={"count": 8}).json()
        assert assigned["drawn"] == 8

        queue = client.get("/api/queue").json()
        assert queue["total"] == 8 and queue["remaining"] == 8

        item = queue["items"][0]
        # Same-origin and signed — never a Drive link, and never carrying a
        # configured hostname that could go stale and break every image.
        assert item["image_url"].startswith("/img/")
        assert "drive.google" not in item["image_url"]
        assert "sig=" in item["image_url"] and "exp=" in item["image_url"]

        response = client.post(
            "/api/review",
            json={"record_id": item["record_id"], "verdict": "correct"},
        )
        assert response.status_code == 200, response.text

        assert client.get("/api/queue").json()["remaining"] == 7
        assert client.get("/api/progress").json()["reviewed"] == 1

    def test_the_default_batch_size_is_used_when_none_is_given(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        assert client.post("/api/queue/assign", json={}).json()["drawn"] == 20

    def test_claiming_stops_at_the_edge_of_the_study(self, client, tmp_path):
        """The other images in the corpus are not the audit and must not leak in."""
        seed_corpus(client, tmp_path)
        result = client.post("/api/queue/assign", json={"count": 500}).json()
        assert result["drawn"] == 24  # SAMPLE_SIZE, not the 40-record corpus
        assert "message" in result

    def test_reviewing_is_refused_before_a_study_is_drawn(self, client, tmp_path):
        seed_corpus(client, tmp_path, sample=False)
        response = client.post("/api/queue/assign", json={"count": 5})
        assert response.status_code == 400
        assert "No study sample" in response.json()["detail"]

    def test_marking_a_label_wrong_needs_the_correction(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 3})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]

        response = client.post(
            "/api/review", json={"record_id": record_id, "verdict": "wrong"}
        )
        assert response.status_code == 400

        response = client.post(
            "/api/review",
            json={"record_id": record_id, "verdict": "wrong", "corrected": "正確"},
        )
        assert response.status_code == 200

    def test_an_unknown_verdict_lists_the_valid_ones(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 1})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        response = client.post(
            "/api/review", json={"record_id": record_id, "verdict": "maybe"}
        )
        assert response.status_code == 400
        assert "correct" in response.json()["detail"]

    def test_a_broken_image_is_swapped_for_a_fresh_one(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 3})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]

        response = client.post(
            "/api/review",
            json={"record_id": record_id, "verdict": "not_an_image"},
        )
        assert response.json()["released"] is True

        queue = client.get("/api/queue").json()
        # Still three to work on: the broken one left, a replacement arrived.
        assert queue["total"] == 3
        assert record_id not in {i["record_id"] for i in queue["items"]}


class TestSkip:
    def test_skipping_swaps_the_image_and_returns_the_replacement(
        self, client, tmp_path
    ):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 3})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]

        result = client.post("/api/queue/skip", json={"record_id": record_id}).json()
        assert result["skipped"] == record_id
        assert result["replacement"]["record_id"] != record_id

        queue = client.get("/api/queue").json()
        assert queue["total"] == 3
        assert record_id not in {i["record_id"] for i in queue["items"]}

    def test_the_study_target_is_untouched(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 3})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        client.post("/api/queue/skip", json={"record_id": record_id})

        progress = client.get("/api/progress").json()
        assert progress["target"] == 24
        assert progress["sample"]["active"] == 24
        assert progress["sample"]["dropped_by_reason"]["not_wanted"] == 1

    def test_skipping_is_attributed_and_does_not_count_as_review(
        self, client, tmp_path
    ):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 3})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        client.post("/api/queue/skip", json={"record_id": record_id})

        progress = client.get("/api/progress").json()
        row = {r["username"]: r for r in progress["reviewers"]}["root"]
        assert row["skipped"] == 1
        assert progress["reviewed"] == 0

    def test_someone_elses_image_cannot_be_skipped(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/queue/assign", json={"count": 3})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]

        client.post("/api/auth/logout")
        login(client, "mai", "password123")
        response = client.post("/api/queue/skip", json={"record_id": record_id})
        assert response.status_code == 400
        assert "root" in response.json()["detail"]

    def test_reviewers_hold_disjoint_queues(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/queue/assign", json={"count": 10})
        mine = {i["record_id"] for i in client.get("/api/queue").json()["items"]}

        client.post("/api/auth/logout")
        login(client, "mai", "password123")
        client.post("/api/queue/assign", json={"count": 10})
        theirs = {i["record_id"] for i in client.get("/api/queue").json()["items"]}

        assert mine and theirs and not (mine & theirs)

    def test_a_reviewer_cannot_judge_someone_elses_image(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/queue/assign", json={"count": 5})
        stolen = client.get("/api/queue").json()["items"][0]["record_id"]

        client.post("/api/auth/logout")
        login(client, "mai", "password123")
        response = client.post(
            "/api/review", json={"record_id": stolen, "verdict": "correct"}
        )
        assert response.status_code == 400
        assert "root" in response.json()["detail"]

    def test_everyone_sees_everyone_elses_finished_work(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/queue/assign", json={"count": 2})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        client.post("/api/review", json={"record_id": record_id, "verdict": "correct"})

        client.post("/api/auth/logout")
        login(client, "mai", "password123")
        shared = client.get("/api/reviewed").json()
        assert shared["total"] == 1
        assert shared["items"][0]["review"]["username"] == "root"

    def test_the_shared_view_can_be_filtered_by_reviewer(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 2})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        client.post("/api/review", json={"record_id": record_id, "verdict": "correct"})

        assert client.get("/api/reviewed?reviewer=root").json()["total"] == 1
        assert client.get("/api/reviewed?reviewer=mai").json()["total"] == 0


class TestProgressPanel:
    def test_the_bar_measures_the_study_not_what_was_claimed(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 4})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        client.post("/api/review", json={"record_id": record_id, "verdict": "correct"})

        progress = client.get("/api/progress").json()
        assert progress["target"] == 24
        assert progress["assigned"] == 4
        assert progress["reviewed"] == 1
        # 1 of the 24-image study, not 25% of the 4 that were claimed.
        assert progress["percent"] == round(100 / 24, 1)

    def test_reports_the_band_plan_against_actual_fill(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 24})

        progress = client.get("/api/progress").json()
        bands = {b["band"]: b for b in progress["bands"]}
        assert bands["exact"]["target_share"] == 0.15
        assert sum(b["in_sample"] for b in bands.values()) == 24
        assert sum(b["assigned"] for b in bands.values()) == 24
        assert progress["reviewers"][0]["username"] == "root"

    def test_reports_the_study_status(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        sample = client.get("/api/progress").json()["sample"]
        assert sample["exists"] and sample["size"] == 24 and sample["active"] == 24

    def test_exposes_the_configured_sampling_plan(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        status = client.get("/api/progress").json()["status"]
        assert status["sampling"]["sample_size"] == 24
        assert status["sampling"]["default_batch"] == 20
        assert status["sampling"]["per_post_cap"] == 2


class TestStudySample:
    def test_only_an_admin_can_draw_the_study(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/auth/logout")
        login(client, "mai", "password123")
        assert client.post("/api/sample", json={}).status_code == 403

    def test_an_explicit_size_overrides_the_default(self, client, tmp_path):
        seed_corpus(client, tmp_path, sample=False)
        result = client.post("/api/sample", json={"size": 10}).json()
        assert result["added"] == 10
        assert client.get("/api/progress").json()["target"] == 10

    def test_a_flagged_image_is_replaced_so_the_study_stays_whole(
        self, client, tmp_path
    ):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 3})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        client.post(
            "/api/review",
            json={"record_id": record_id, "verdict": "not_an_image"},
        )
        sample = client.get("/api/progress").json()["sample"]
        assert sample["active"] == 24
        assert sample["dropped"] == 1

    def test_top_up_is_available_to_an_admin(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        assert client.post("/api/sample/top-up").json()["added"] == 0

    def test_images_missing_from_the_drive_index_are_never_drawn(
        self, client, tmp_path
    ):
        """A truncated index costs coverage; it must not cost reviewer time."""
        seed_corpus(client, tmp_path, sample=False, indexed=range(12))
        result = client.post("/api/sample", json={"size": 24}).json()
        assert result["added"] == 12
        assert client.get("/api/progress").json()["corpus"]["drawable"] == 12

    def test_drawing_without_an_indexed_folder_explains_itself(
        self, client, tmp_path
    ):
        seed_corpus(client, tmp_path, sample=False, indexed=[])
        response = client.post("/api/sample", json={})
        assert response.status_code == 400
        assert "Drive index" in response.json()["detail"]


class TestExports:
    def _one_review(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 3})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        client.post(
            "/api/review",
            json={
                "record_id": record_id,
                "verdict": "minor",
                "corrected": "年歲漸增",
                "note": "variant form",
                "gemini_verdict": "partial",
            },
        )

    def test_jsonl_export(self, client, tmp_path):
        self._one_review(client, tmp_path)
        response = client.get("/api/export/reviews.jsonl")
        assert response.status_code == 200
        row = json.loads(response.text.strip().splitlines()[0])
        assert row["verdict"] == "minor"
        assert row["corrected"] == "年歲漸增"
        assert row["reviewer"] == "root"

    def test_csv_export_opens_cleanly_in_excel(self, client, tmp_path):
        self._one_review(client, tmp_path)
        response = client.get("/api/export/reviews.csv")
        assert response.status_code == 200
        # The BOM is what stops Excel rendering the CJK as mojibake.
        assert response.content.startswith(b"\xef\xbb\xbf")
        assert "年歲漸增" in response.content.decode("utf-8-sig")

    def test_xlsx_export(self, client, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        import io

        self._one_review(client, tmp_path)
        response = client.get("/api/export/reviews.xlsx")
        assert response.status_code == 200

        book = openpyxl.load_workbook(io.BytesIO(response.content))
        rows = list(book["reviews"].iter_rows(values_only=True))
        header, first = rows[0], rows[1]
        assert "corrected" in header
        assert "年歲漸增" in first
        # Accuracy stays a number, so the column can be averaged.
        accuracy = first[header.index("ground_truth_cer_accuracy")]
        assert isinstance(accuracy, float)

    def test_danh_gia_export(self, client, tmp_path):
        """The upstream team's own format, served from the same reviewed rows."""
        openpyxl = pytest.importorskip("openpyxl")
        import io

        self._one_review(client, tmp_path)
        response = client.get("/api/export/danh_gia.xlsx")
        assert response.status_code == 200

        book = openpyxl.load_workbook(io.BytesIO(response.content))
        sheet = book["Đánh giá"]
        assert [c.value for c in sheet[1]] == [
            "Post ID", "Image", "FB Caption", "Label", "Corrected",
            "Levenshtein Accuracy",
            "Levenshtein Accuracy (bao gồm cả line break)", "Note",
        ]
        # Label is their transcription, Corrected is the reviewer's.
        assert sheet.cell(row=2, column=4).value == "年歲漸長"
        assert sheet.cell(row=2, column=5).value == "年歲漸增"
        assert isinstance(sheet.cell(row=2, column=6).value, float)
        assert sheet.cell(row=2, column=6).number_format == "0.00%"

    def test_danh_gia_needs_a_session(self, client, tmp_path):
        self._one_review(client, tmp_path)
        client.post("/api/auth/logout")
        assert client.get("/api/export/danh_gia.xlsx").status_code == 401



class TestImageRoute:
    def test_an_unsigned_request_is_refused(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        response = client.get(f"/img/{POST_A[:-2]}/0.jpg?exp=99999999999&sig=bad")
        assert response.status_code == 403

    def test_a_traversal_attempt_is_refused(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        response = client.get("/img/..%2F..%2Fetc/0.jpg?exp=1&sig=x")
        assert response.status_code in (400, 403, 404)


class TestExtendAndReassign:
    def test_extending_grows_the_target_without_losing_work(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 5})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        client.post("/api/review", json={"record_id": record_id, "verdict": "correct"})

        result = client.post("/api/sample/extend", json={"count": 8}).json()
        assert result["added"] == 8
        assert result["target"] == 32

        progress = client.get("/api/progress").json()
        assert progress["target"] == 32
        assert progress["reviewed"] == 1          # the review survived
        assert progress["assigned"] == 5          # the claims survived
        assert progress["unclaimed"] == 27

    def test_extending_needs_a_study_first(self, client, tmp_path):
        seed_corpus(client, tmp_path, sample=False)
        response = client.post("/api/sample/extend", json={"count": 5})
        assert response.status_code == 400
        assert "Draw a study sample" in response.json()["detail"]

    def test_only_an_admin_can_extend(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/auth/logout")
        login(client, "mai", "password123")
        assert client.post("/api/sample/extend", json={"count": 5}).status_code == 403

    def test_reassigning_moves_unreviewed_work(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/queue/assign", json={"count": 6})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]
        client.post("/api/review", json={"record_id": record_id, "verdict": "correct"})

        result = client.post(
            "/api/queue/reassign", json={"from_user": "root", "to_user": "mai"}
        ).json()
        assert result["moved"] == 5

        rows = {r["username"]: r for r in client.get("/api/progress").json()["reviewers"]}
        assert rows["root"]["reviewed"] == 1
        assert rows["root"]["assigned"] == 1     # the reviewed one stays put
        assert rows["mai"]["assigned"] == 5

    def test_reassigning_to_an_unknown_user_is_refused(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 3})
        response = client.post(
            "/api/queue/reassign", json={"from_user": "root", "to_user": "ghost"}
        )
        assert response.status_code == 400
        assert "ghost" in response.json()["detail"]

    def test_only_an_admin_can_reassign(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/users", json={"username": "mai", "password": "password123"})
        client.post("/api/auth/logout")
        login(client, "mai", "password123")
        response = client.post(
            "/api/queue/reassign", json={"from_user": "mai", "to_user": "root"}
        )
        assert response.status_code == 403
