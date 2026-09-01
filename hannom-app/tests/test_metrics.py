from app.core.metrics import compare, levenshtein, normalize


def test_levenshtein_basics():
    assert levenshtein("", "") == 0
    assert levenshtein("abc", "abc") == 0
    assert levenshtein("abc", "") == 3
    assert levenshtein("kitten", "sitting") == 3


def test_levenshtein_is_symmetric():
    a, b = "年歲漸長", "年歲漸增長"
    assert levenshtein(a, b) == levenshtein(b, a)


def test_counts_cjk_by_character_not_byte():
    # 10 characters, not 30 bytes: a byte-wise distance would make every CJK
    # score meaningless.
    text = "年歲漸長心要活得自由"
    assert len(normalize(text)) == 10
    assert compare(text, text).reference_len == 10


def test_whitespace_is_layout_not_content():
    assert compare("年歲漸長\n心要活得自由", "年歲漸長 心要活得自由").exact
    # ...unless the caller asks for it to count.
    assert not compare(
        "年歲漸長\n心要活得自由",
        "年歲漸長 心要活得自由",
        strip_whitespace=False,
    ).exact


def test_nfc_folding_treats_equivalent_forms_as_equal():
    composed = "à"           # à
    decomposed = "à"        # a + combining grave
    assert compare(composed, decomposed).exact


def test_both_empty_is_a_perfect_match():
    result = compare("", "")
    assert result.exact and result.cer_accuracy == 1.0


def test_one_empty_is_a_total_miss():
    result = compare("年歲漸長", "")
    assert result.cer_accuracy == 0.0
    assert result.max_accuracy == 0.0


def test_cer_is_stricter_than_max_when_the_model_over_produces():
    """The divergence that justifies reporting both denominators.

    A hallucinated tail is punished by CER and softened by max-length, which is
    exactly the failure this audit exists to catch.
    """
    reference = "年歲漸長"
    hypothesis = "年歲漸長心要活得自由更多字句"
    result = compare(reference, hypothesis)
    assert result.cer_accuracy < result.max_accuracy


def test_scores_never_go_negative():
    result = compare("一", "一二三四五六七八九十")
    assert result.cer_accuracy == 0.0
    assert 0.0 <= result.max_accuracy <= 1.0


def test_single_substitution_scores_as_expected():
    result = compare("年歲漸長", "年歲漸增")
    assert result.distance == 1
    assert result.cer_accuracy == 0.75
    assert result.max_accuracy == 0.75
