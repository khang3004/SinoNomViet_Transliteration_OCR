from app.core.postid import (
    decode,
    parse_image_name,
    permalink_for,
    slug,
    unslug,
)

# The case the upstream team's filenames are built from.
SAMPLE = "UzpfSTEwMDAwMDU5MzExMzI1ODpWSzoyNzgzNTQ4OTgyNjA5MzEwMA=="
EXPECTED = (
    "https://www.facebook.com/permalink.php"
    "?story_fbid=27835489826093100&id=100000593113258"
)


def test_decodes_the_reference_post_id():
    assert permalink_for(SAMPLE) == EXPECTED


def test_decoded_parts():
    story = decode(SAMPLE)
    assert story.decoded == "S:_I100000593113258:VK:27835489826093100"
    assert story.owner_id == "100000593113258"
    assert story.story_fbid == "27835489826093100"


def test_two_part_form_without_vk():
    """S:_I<owner>:<story> — the shape used off personal timelines."""
    import base64

    raw = "S:_I100000593113258:27835489826093100"
    encoded = base64.b64encode(raw.encode()).decode()
    assert permalink_for(encoded) == EXPECTED


def test_unpadded_and_urlsafe_input_still_decodes():
    assert decode(SAMPLE.rstrip("=")).owner_id == "100000593113258"


def test_garbage_yields_no_permalink_rather_than_a_wrong_one():
    """A half-built URL would send a reviewer to the wrong post silently."""
    assert permalink_for("not base64 at all!!") == ""
    assert permalink_for("") == ""


def test_single_numeric_token_is_not_treated_as_a_permalink():
    import base64

    encoded = base64.b64encode(b"S:_I100000593113258").decode()
    story = decode(encoded)
    assert story.permalink == ""


def test_slug_is_url_and_path_safe():
    dirty = "ab+cd/ef=="
    assert slug(dirty) == "ab-cd_ef"
    assert "/" not in slug(dirty) and "=" not in slug(dirty)


def test_slug_roundtrips():
    assert unslug(slug(SAMPLE)) == SAMPLE


def test_slugged_id_still_decodes():
    """The slug must survive a round trip through a URL and still be readable."""
    assert permalink_for(unslug(slug(SAMPLE))) == EXPECTED


def test_parse_image_name_matches_their_convention():
    assert parse_image_name(f"{SAMPLE}_0.jpg") == (SAMPLE, 0, ".jpg")
    assert parse_image_name(f"images/{SAMPLE}_12.PNG") == (SAMPLE, 12, ".png")


def test_parse_image_name_needs_a_numeric_index():
    assert parse_image_name("notes.txt") is None
    assert parse_image_name("no_index.jpg") is None
    assert parse_image_name(f"{SAMPLE}.jpg") is None
    assert parse_image_name("") is None


def test_parse_splits_on_the_last_underscore():
    """Standard base64 has no underscore, but a slug does — both must parse."""
    slugged = slug(SAMPLE)
    assert parse_image_name(f"{slugged}_3.jpg") == (slugged, 3, ".jpg")
