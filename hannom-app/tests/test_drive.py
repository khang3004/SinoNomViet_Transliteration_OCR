import json

import pytest

from app.core.drive import (
    EMBEDDED_VIEW_LIMIT,
    DriveError,
    DriveImages,
    DriveIndex,
    ServiceAccount,
    download_url,
    file_id_from,
    folder_id_from,
)

FOLDER = "1sdtPaCUhEV225RJkXTQiF-5Ww48pxTsh"
FILE_ID = "1Lof1RqNoaEgaUZZUQ0C879XNOqgAFEfd"
NAME = "UzpfSTE0MTAzODU0MjQ6Vks6Mjc2ODU2MTY5Mjc3NDcwNTg=_0.jpg"


def entry_html(file_id: str, name: str) -> str:
    """One entry, in the shape the public folder page actually emits."""
    return (
        f'<div class="flip-entry" id="entry-{file_id}"><div class="flip-entry-info">'
        f'<a href="https://drive.google.com/file/d/{file_id}/view?usp=drive_web">'
        f'<div class="flip-entry-visual"><img src="https://lh3.example/x=s190"/></div>'
        f'<div class="flip-entry-title">{name}</div></a></div></div>'
    )


def page(entries) -> str:
    return "<html><body>" + "".join(entry_html(i, n) for i, n in entries) + "</body></html>"


class FakeResponse:
    def __init__(self, status=200, text="", content=b"", headers=None, payload=None):
        self.status_code = status
        self.text = text
        self.content = content
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.text)


class FakeClient:
    """Returns queued responses and records what was asked for."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)

    async def aclose(self):
        pass


class TestIdParsing:
    def test_folder_id_from_a_full_url(self):
        assert folder_id_from(f"https://drive.google.com/drive/folders/{FOLDER}") == FOLDER

    def test_folder_id_from_a_bare_id(self):
        assert folder_id_from(FOLDER) == FOLDER

    def test_folder_id_from_a_url_with_query(self):
        assert folder_id_from(
            f"https://drive.google.com/drive/folders/{FOLDER}?usp=sharing"
        ) == FOLDER

    def test_garbage_yields_nothing(self):
        assert folder_id_from("not a folder") == ""
        assert folder_id_from("") == ""

    def test_file_id_from_a_view_url(self):
        assert file_id_from(f"https://drive.google.com/file/d/{FILE_ID}/view") == FILE_ID

    def test_download_url_is_the_anonymous_one(self):
        # Downloads never authenticate: a file in a public folder is readable
        # without credentials, which is what makes the public index usable.
        assert download_url(FILE_ID) == (
            f"https://drive.google.com/uc?export=download&id={FILE_ID}"
        )


class TestPublicListing:
    @pytest.mark.asyncio
    async def test_parses_id_and_filename_pairs(self, tmp_path):
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        client = FakeClient([FakeResponse(text=page([(FILE_ID, NAME)]))])

        report = await index.refresh(client)

        assert report.method == "public_page"
        assert report.images == 1
        assert index.file_id_for(NAME) == FILE_ID

    @pytest.mark.asyncio
    async def test_needs_no_credentials(self, tmp_path):
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        client = FakeClient([FakeResponse(text=page([(FILE_ID, NAME)]))])
        await index.refresh(client)

        _, url, kwargs = client.calls[0]
        assert "embeddedfolderview" in url
        assert "Authorization" not in kwargs.get("headers", {})

    @pytest.mark.asyncio
    async def test_non_images_are_skipped(self, tmp_path):
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        client = FakeClient([
            FakeResponse(text=page([(FILE_ID, NAME), ("otherid" + "x" * 20, "notes.txt")]))
        ])
        report = await index.refresh(client)
        assert report.images == 1 and report.skipped_non_image == 1

    @pytest.mark.asyncio
    async def test_a_full_page_is_reported_as_truncated(self, tmp_path):
        """The endpoint stops at its cap silently; a wrong total is worse than
        an admitted incomplete one."""
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        entries = [(f"id{i:029d}", f"file{i}.jpg") for i in range(EMBEDDED_VIEW_LIMIT)]
        client = FakeClient([FakeResponse(text=page(entries))])

        report = await index.refresh(client)
        assert report.images == EMBEDDED_VIEW_LIMIT
        assert report.truncated is True

    @pytest.mark.asyncio
    async def test_a_short_page_is_not_truncated(self, tmp_path):
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        client = FakeClient([FakeResponse(text=page([(FILE_ID, NAME)]))])
        assert (await index.refresh(client)).truncated is False

    @pytest.mark.asyncio
    async def test_an_empty_listing_explains_itself(self, tmp_path):
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        client = FakeClient([FakeResponse(text="<html><body></body></html>")])
        report = await index.refresh(client)
        assert "not be shared publicly" in report.error

    @pytest.mark.asyncio
    async def test_an_unconfigured_folder_is_not_an_exception(self, tmp_path):
        report = await DriveIndex(tmp_path / "idx.json").refresh(FakeClient([]))
        assert "GOOGLE_DRIVE_FOLDER_ID" in report.error


class TestServiceAccountListing:
    KEY = json.dumps({
        "client_email": "svc@example.iam.gserviceaccount.com",
        "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
        "token_uri": "https://oauth2.googleapis.com/token",
    })

    def test_loads_from_inline_json(self):
        account = ServiceAccount.load(self.KEY)
        assert account.client_email == "svc@example.iam.gserviceaccount.com"

    def test_loads_from_a_file_path(self, tmp_path):
        path = tmp_path / "sa.json"
        path.write_text(self.KEY, encoding="utf-8")
        assert ServiceAccount.load(str(path)) is not None

    def test_an_empty_source_is_simply_absent(self):
        assert ServiceAccount.load("") is None

    def test_the_wrong_json_is_rejected_clearly(self):
        with pytest.raises(DriveError, match="not a service-account key"):
            ServiceAccount.load(json.dumps({"api_key": "AIza..."}))

    def test_a_missing_file_is_reported(self, tmp_path):
        with pytest.raises(DriveError, match="Could not read"):
            ServiceAccount.load(str(tmp_path / "nope.json"))

    @pytest.mark.asyncio
    async def test_pages_through_the_whole_folder(self, tmp_path, monkeypatch):
        """The reason a service account exists: files.list pages, the public
        page does not."""
        monkeypatch.setattr(
            "jwt.encode", lambda *a, **k: "assertion", raising=False
        )
        index = DriveIndex(
            tmp_path / "idx.json", folder_id=FOLDER, service_account=self.KEY
        )
        client = FakeClient([
            FakeResponse(payload={"access_token": "tok", "expires_in": 3600}),
            FakeResponse(payload={
                "nextPageToken": "p2",
                "files": [{"id": "a" * 25, "name": "a.jpg", "mimeType": "image/jpeg"}],
            }),
            FakeResponse(payload={
                "files": [{"id": "b" * 25, "name": "b.jpg", "mimeType": "image/jpeg"}],
            }),
        ])

        report = await index.refresh(client)

        assert report.method == "service_account"
        assert report.pages == 2 and report.images == 2
        assert report.truncated is False
        assert index.file_id_for("b.jpg") == "b" * 25

    @pytest.mark.asyncio
    async def test_the_bearer_token_is_sent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("jwt.encode", lambda *a, **k: "assertion", raising=False)
        index = DriveIndex(
            tmp_path / "idx.json", folder_id=FOLDER, service_account=self.KEY
        )
        client = FakeClient([
            FakeResponse(payload={"access_token": "tok", "expires_in": 3600}),
            FakeResponse(payload={"files": []}),
        ])
        await index.refresh(client)

        list_call = [c for c in client.calls if c[1].endswith("/files")][0]
        assert list_call[2]["headers"]["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_a_403_names_the_likely_cause(self, tmp_path, monkeypatch):
        monkeypatch.setattr("jwt.encode", lambda *a, **k: "assertion", raising=False)
        index = DriveIndex(
            tmp_path / "idx.json", folder_id=FOLDER, service_account=self.KEY
        )
        client = FakeClient([
            FakeResponse(payload={"access_token": "tok", "expires_in": 3600}),
            FakeResponse(status=403, text="forbidden"),
        ])
        report = await index.refresh(client)
        assert "share the folder with the service-account email" in report.error


class TestMirror:
    @pytest.mark.asyncio
    async def test_downloads_and_stores_an_image(self, tmp_path):
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        index._entries = {NAME: {"id": FILE_ID, "mime": "image/jpeg", "size": 10}}
        images = DriveImages(index, tmp_path / "images")
        client = FakeClient([
            FakeResponse(content=b"\xff\xd8jpegbytes", headers={"content-type": "image/jpeg"})
        ])

        path = await images.ensure("post", 0, NAME, ".jpg", client)
        assert path.read_bytes() == b"\xff\xd8jpegbytes"

    @pytest.mark.asyncio
    async def test_a_cached_file_is_not_refetched(self, tmp_path):
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        index._entries = {NAME: {"id": FILE_ID}}
        images = DriveImages(index, tmp_path / "images")
        client = FakeClient([
            FakeResponse(content=b"bytes", headers={"content-type": "image/jpeg"})
        ])
        await images.ensure("post", 0, NAME, ".jpg", client)

        # No responses left: a second fetch would raise IndexError.
        again = await images.ensure("post", 0, NAME, ".jpg", client)
        assert again.exists() and len(client.calls) == 1

    @pytest.mark.asyncio
    async def test_an_html_interstitial_is_refused_not_stored(self, tmp_path):
        """Drive serves a page instead of bytes when a file is not public.
        Storing it would leave a broken thumbnail with no explanation."""
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        index._entries = {NAME: {"id": FILE_ID}}
        images = DriveImages(index, tmp_path / "images")
        client = FakeClient([
            FakeResponse(content=b"<html>sign in</html>",
                         headers={"content-type": "text/html"})
        ])
        with pytest.raises(DriveError, match="publicly shared"):
            await images.ensure("post", 0, NAME, ".jpg", client)
        assert images.cached("post", 0) is None

    @pytest.mark.asyncio
    async def test_an_unindexed_name_says_what_to_do(self, tmp_path):
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        images = DriveImages(index, tmp_path / "images")
        with pytest.raises(DriveError, match="not in the Drive index"):
            await images.ensure("post", 0, "missing.jpg", ".jpg", FakeClient([]))

    @pytest.mark.asyncio
    async def test_an_oversized_file_is_refused(self, tmp_path):
        index = DriveIndex(tmp_path / "idx.json", folder_id=FOLDER)
        index._entries = {NAME: {"id": FILE_ID}}
        images = DriveImages(index, tmp_path / "images", max_bytes=4)
        client = FakeClient([
            FakeResponse(content=b"much too long", headers={"content-type": "image/jpeg"})
        ])
        with pytest.raises(DriveError, match="over the cap"):
            await images.ensure("post", 0, NAME, ".jpg", client)
