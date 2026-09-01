import json

import pytest

from app.core import corpus
from app.core.models import Band

POST_A = "UzpfSTEwMDAwMDU5MzExMzI1ODpWSzoyNzgzNTQ4OTgyNjA5MzEwMA=="
POST_B = "UzpfSTE0MTAzODU0MjQ6Vks6Mjc2ODU2MTY5Mjc3NDcwNTg="


def write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


def write_xlsx(path, header, rows):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    book.save(path)
    return path


def jsonl_row(image, ground_truth="年歲漸長", gemini="年歲漸長", deepseek="年歲"):
    return {
        "image": image,
        "ground_truth": ground_truth,
        "label": "label text",
        "gemini": [{"text": gemini}],
        "deepseek": [{"text": deepseek}],
    }


class TestJsonl:
    def test_reads_model_arrays(self, tmp_path):
        path = write_jsonl(tmp_path / "gt.jsonl", [jsonl_row(f"{POST_A}_0.jpg")])
        rows = corpus.read_jsonl(path)
        entry = rows[f"{POST_A}_0.jpg"]
        assert entry["gemini"] == "年歲漸長"
        assert entry["deepseek"] == "年歲"

    def test_takes_the_first_non_empty_model_text(self, tmp_path):
        row = jsonl_row(f"{POST_A}_0.jpg")
        row["gemini"] = [{"text": ""}, {"text": "第二"}]
        path = write_jsonl(tmp_path / "gt.jsonl", [row])
        assert corpus.read_jsonl(path)[f"{POST_A}_0.jpg"]["gemini"] == "第二"

    def test_directory_prefixes_are_stripped_so_files_join(self, tmp_path):
        path = write_jsonl(
            tmp_path / "gt.jsonl", [jsonl_row(f"images/{POST_A}_0.jpg")]
        )
        assert f"{POST_A}_0.jpg" in corpus.read_jsonl(path)

    def test_a_torn_line_does_not_lose_the_file(self, tmp_path):
        path = tmp_path / "gt.jsonl"
        path.write_text(
            json.dumps(jsonl_row(f"{POST_A}_0.jpg")) + "\n{ broken\n",
            encoding="utf-8",
        )
        assert len(corpus.read_jsonl(path)) == 1


class TestXlsx:
    HEADER = ["post_id", "image", "caption", "ground_truth", "gemini_ocr", "post_link"]

    def test_reads_their_six_columns(self, tmp_path):
        path = write_xlsx(
            tmp_path / "gt.xlsx",
            self.HEADER,
            [[POST_A, f"{POST_A}_0.jpg", "caption", "年歲漸長", "年歲漸增", "https://fb/1"]],
        )
        entry = corpus.read_xlsx(path)[f"{POST_A}_0.jpg"]
        assert entry["post_id"] == POST_A
        assert entry["caption"] == "caption"
        assert entry["post_link"] == "https://fb/1"

    def test_header_case_and_spacing_are_tolerated(self, tmp_path):
        path = write_xlsx(
            tmp_path / "gt.xlsx",
            ["Post ID", "Image", "FB Caption", "Ground Truth", "Gemini OCR", "Link"],
            [[POST_A, f"{POST_A}_0.jpg", "cap", "年", "年", "https://fb/1"]],
        )
        assert corpus.read_xlsx(path)[f"{POST_A}_0.jpg"]["caption"] == "cap"

    def test_a_missing_image_column_is_a_clear_error(self, tmp_path):
        path = write_xlsx(tmp_path / "gt.xlsx", ["a", "b"], [["1", "2"]])
        with pytest.raises(ValueError, match="no 'image' column"):
            corpus.read_xlsx(path)

    def test_empty_cells_do_not_become_the_string_nan(self, tmp_path):
        """A blank correction that reads back as 'nan' would be scored as text."""
        path = write_xlsx(
            tmp_path / "gt.xlsx",
            self.HEADER,
            [[POST_A, f"{POST_A}_0.jpg", None, "年歲漸長", None, None]],
        )
        entry = corpus.read_xlsx(path)[f"{POST_A}_0.jpg"]
        assert entry["caption"] == ""
        assert entry["gemini"] == ""


class TestClassify:
    def test_identical_is_exact(self):
        band, _ = corpus.classify("年歲漸長", "年歲漸長")
        assert band is Band.EXACT

    def test_line_breaks_alone_still_count_as_exact(self):
        band, _ = corpus.classify("年歲漸長\n心要活得自由", "年歲漸長 心要活得自由")
        assert band is Band.EXACT

    def test_one_character_off_in_twenty_is_near(self):
        band, _ = corpus.classify("一二三四五六七八九十" * 2, "一二三四五六七八九十" * 2 + "誤")
        assert band is Band.NEAR

    def test_half_wrong_is_far(self):
        band, _ = corpus.classify("一二三四五六七八", "一二三四億兆京垓")
        assert band is Band.FAR

    def test_mostly_wrong_is_poor(self):
        band, _ = corpus.classify("一二三四五六七八", "億兆京垓秭穰溝澗")
        assert band is Band.POOR

    def test_a_blank_side_is_its_own_band(self):
        assert corpus.classify("年歲漸長", "")[0] is Band.EMPTY
        assert corpus.classify("", "年歲漸長")[0] is Band.EMPTY


class TestBuild:
    HEADER = TestXlsx.HEADER

    def test_merges_the_two_files_on_the_image_name(self, tmp_path):
        image = f"{POST_A}_0.jpg"
        jsonl = write_jsonl(tmp_path / "gt.jsonl", [jsonl_row(image)])
        xlsx = write_xlsx(
            tmp_path / "gt.xlsx",
            self.HEADER,
            [[POST_A, image, "a caption", "年歲漸長", "年歲漸長", "https://fb/1"]],
        )
        records, report = corpus.build(jsonl, xlsx)

        assert report.matched == 1
        assert len(records) == 1
        record = records[0]
        # Model output from the jsonl, post identity from the xlsx.
        assert record.deepseek == "年歲"
        assert record.caption == "a caption"
        assert record.sources == ["jsonl", "xlsx"]

    def test_either_file_alone_still_produces_records(self, tmp_path):
        jsonl = write_jsonl(tmp_path / "gt.jsonl", [jsonl_row(f"{POST_A}_0.jpg")])
        records, report = corpus.build(jsonl, None)
        assert len(records) == 1 and report.jsonl_only == 1

    def test_post_id_and_link_are_derived_when_the_xlsx_is_absent(self, tmp_path):
        jsonl = write_jsonl(tmp_path / "gt.jsonl", [jsonl_row(f"{POST_A}_0.jpg")])
        record = corpus.build(jsonl, None)[0][0]
        assert record.post_id == POST_A
        assert record.post_link.endswith("story_fbid=27835489826093100&id=100000593113258")

    def test_rows_that_are_not_pictures_never_enter_the_corpus(self, tmp_path):
        jsonl = write_jsonl(
            tmp_path / "gt.jsonl",
            [jsonl_row(f"{POST_A}_0.jpg"), jsonl_row("notes.txt"), jsonl_row("")],
        )
        records, report = corpus.build(jsonl, None)
        assert len(records) == 1
        assert report.skipped["not_an_image_file"] == 1

    def test_rows_without_a_ground_truth_are_skipped(self, tmp_path):
        """The task is auditing their transcription; a blank one has none."""
        jsonl = write_jsonl(
            tmp_path / "gt.jsonl", [jsonl_row(f"{POST_A}_0.jpg", ground_truth="")]
        )
        records, report = corpus.build(jsonl, None)
        assert records == [] and report.skipped["no_ground_truth"] == 1

    def test_multiple_images_from_one_post_are_separate_records(self, tmp_path):
        jsonl = write_jsonl(
            tmp_path / "gt.jsonl",
            [jsonl_row(f"{POST_A}_{i}.jpg") for i in range(3)],
        )
        records, _ = corpus.build(jsonl, None)
        assert len(records) == 3
        assert {r.idx for r in records} == {0, 1, 2}
        assert len({r.post_id for r in records}) == 1

    def test_record_ids_are_stable_and_path_safe(self, tmp_path):
        jsonl = write_jsonl(tmp_path / "gt.jsonl", [jsonl_row(f"{POST_A}_0.jpg")])
        first = corpus.build(jsonl, None)[0][0].record_id
        second = corpus.build(jsonl, None)[0][0].record_id
        assert first == second
        assert "/" not in first and "+" not in first and "=" not in first

    def test_bands_are_counted_in_the_report(self, tmp_path):
        jsonl = write_jsonl(
            tmp_path / "gt.jsonl",
            [
                jsonl_row(f"{POST_A}_0.jpg", gemini="年歲漸長"),
                jsonl_row(f"{POST_B}_0.jpg", gemini=""),
            ],
        )
        _, report = corpus.build(jsonl, None)
        assert report.bands["exact"] == 1
        assert report.bands["empty"] == 1
