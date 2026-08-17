"""HMAC image-URL signing and path containment.

``/img/*`` is the only unauthenticated route — the Gemini stage fetches it
anonymously — so these are the checks standing between a signed URL scheme and
an open directory on the public internet.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.imagestore import (
    is_safe_post_id,
    parse_image_filename,
    resolve_image_path,
)
from app.core.signing import ImageUrlSigner, SigningError, sign, verify

SECRET = "test-signing-secret"
FUTURE = int(time.time()) + 3600


class TestSigning:
    def test_valid_signature_verifies(self):
        sig = sign("p1", 0, FUTURE, SECRET)
        assert verify("p1", 0, FUTURE, sig, SECRET) is True

    @pytest.mark.parametrize(
        "post_id,idx,secret",
        [("other", 0, SECRET), ("p1", 1, SECRET), ("p1", 0, "wrong-secret")],
    )
    def test_signature_is_bound_to_every_field(self, post_id, idx, secret):
        sig = sign("p1", 0, FUTURE, SECRET)
        assert verify(post_id, idx, FUTURE, sig, secret) is False

    def test_tampered_signature_rejected(self):
        sig = sign("p1", 0, FUTURE, SECRET)
        flipped = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        assert verify("p1", 0, FUTURE, flipped, SECRET) is False

    def test_expired_signature_rejected_even_though_hmac_is_valid(self):
        past = int(time.time()) - 1
        sig = sign("p1", 0, past, SECRET)
        assert verify("p1", 0, past, sig, SECRET) is False

    @pytest.mark.parametrize("sig,secret", [("", SECRET), ("abc", ""), ("", "")])
    def test_missing_secret_or_signature_rejected(self, sig, secret):
        assert verify("p1", 0, FUTURE, sig, secret) is False

    def test_minting_without_a_secret_is_refused(self):
        # Failing loudly beats minting URLs that anyone could forge.
        with pytest.raises(SigningError):
            sign("p1", 0, FUTURE, "")


class TestSigner:
    def test_builds_a_verifiable_url(self):
        signer = ImageUrlSigner("https://scan.example.com/", SECRET, ttl_days=30)
        url, expires_at = signer.build("p1", 0, ".jpg")

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert parsed.path == "/img/p1/0.jpg"
        assert int(params["exp"][0]) == expires_at
        assert verify("p1", 0, expires_at, params["sig"][0], SECRET) is True

    def test_ttl_is_honoured(self):
        signer = ImageUrlSigner("https://x", SECRET, ttl_days=30)
        _, expires_at = signer.build("p1", 0)
        assert 29 * 86400 < expires_at - int(time.time()) <= 30 * 86400


class TestPostIdSafety:
    @pytest.mark.parametrize("pid", ["p123", "abc_1.2:3", "ok-id", "9" * 128])
    def test_accepts_realistic_ids(self, pid):
        assert is_safe_post_id(pid) is True

    @pytest.mark.parametrize(
        "pid", ["../etc", "a/b", "..", "a\\b", "", "x" * 129, "a b", "p\x00"]
    )
    def test_rejects_separators_traversal_and_overlong(self, pid):
        assert is_safe_post_id(pid) is False


class TestFilenameParsing:
    @pytest.mark.parametrize(
        "filename,expected",
        [("0.jpg", (0, ".jpg")), ("12.png", (12, ".png")), ("0.JPG", (0, ".jpg"))],
    )
    def test_valid_names(self, filename, expected):
        assert parse_image_filename(filename) == expected

    @pytest.mark.parametrize(
        "filename", ["x.jpg", "0.exe", "0", "../0.jpg", "0.", ".jpg"]
    )
    def test_invalid_names(self, filename):
        assert parse_image_filename(filename) is None


class TestPathContainment:
    @pytest.fixture
    def images_dir(self, tmp_path):
        root = tmp_path / "images"
        (root / "p1").mkdir(parents=True)
        (root / "p1" / "p123_0.jpg").write_bytes(b"jpeg")
        (root / "p1" / "p124_0.png").write_bytes(b"png")
        (tmp_path / "secret.txt").write_bytes(b"TOPSECRET")
        return root

    def test_finds_a_stored_image(self, images_dir):
        assert resolve_image_path(images_dir, "p123", 0, ".jpg") is not None

    def test_tolerates_suffix_mismatch(self, images_dir):
        # The URL may say .jpg while the CDN actually served a .png.
        found = resolve_image_path(images_dir, "p124", 0, ".jpg")
        assert found is not None and found.suffix == ".png"

    def test_missing_image_returns_none(self, images_dir):
        assert resolve_image_path(images_dir, "nope", 0, ".jpg") is None

    @pytest.mark.parametrize(
        "evil", ["../secret", "../../secret", "..", "a/../../b", "/etc/passwd"]
    )
    def test_traversal_never_escapes_the_images_dir(self, images_dir, evil):
        assert resolve_image_path(images_dir, evil, 0, ".txt") is None
