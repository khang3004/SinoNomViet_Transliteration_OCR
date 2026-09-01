"""End-to-end over HTTP: sign in, take work, judge it, export it."""

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.core.users import hash_password  # noqa: E402

ADMIN_PASSWORD = "admin-password"
POST_A = "UzpfSTEwMDAwMDU5MzExMzI1ODpWSzoyNzgzNTQ4OTgyNjA5MzEwMA=="


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
                "image": f"{POST_A[:-4]}{i:03d}==_0.jpg",
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
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://review.example")
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


def seed_corpus(client, tmp_path, count=40):
    """Sign in as admin, upload a ground_truth.jsonl, ingest it."""
    login(client, "root", ADMIN_PASSWORD)
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
    return response.json()


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
        # Signed, on our own domain — not a Drive link.
        assert item["image_url"].startswith("https://review.example/img/")
        assert "sig=" in item["image_url"]

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

    def test_a_short_pool_says_so_rather_than_silently_under_delivering(
        self, client, tmp_path
    ):
        seed_corpus(client, tmp_path, count=4)
        result = client.post("/api/queue/assign", json={"count": 50}).json()
        assert result["drawn"] < 50
        assert "message" in result

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

    def test_a_broken_image_frees_a_slot_and_reports_it(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 3})
        record_id = client.get("/api/queue").json()["items"][0]["record_id"]

        response = client.post(
            "/api/review",
            json={"record_id": record_id, "verdict": "not_an_image"},
        )
        assert response.json()["released"] is True
        assert client.get("/api/queue").json()["total"] == 2

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
    def test_reports_the_band_plan_against_actual_fill(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        client.post("/api/queue/assign", json={"count": 20})

        progress = client.get("/api/progress").json()
        assert progress["assigned"] == 20
        bands = {b["band"]: b for b in progress["bands"]}
        assert bands["exact"]["target_share"] == 0.15
        assert sum(b["assigned"] for b in bands.values()) == 20
        assert progress["reviewers"][0]["username"] == "root"

    def test_exposes_the_configured_sampling_plan(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        status = client.get("/api/progress").json()["status"]
        assert status["sampling"]["default_batch"] == 20
        assert status["sampling"]["per_post_cap"] == 2


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


class TestImageRoute:
    def test_an_unsigned_request_is_refused(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        response = client.get(f"/img/{POST_A[:-2]}/0.jpg?exp=99999999999&sig=bad")
        assert response.status_code == 403

    def test_a_traversal_attempt_is_refused(self, client, tmp_path):
        seed_corpus(client, tmp_path)
        response = client.get("/img/..%2F..%2Fetc/0.jpg?exp=1&sig=x")
        assert response.status_code in (400, 403, 404)
