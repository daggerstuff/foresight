"""Signature extraction must key on real error syntax, not on words that look like it."""

from foresight.error_signatures import (
    error_class_signature,
    error_signature_like_prefix,
    normalize_error_signature,
)


def test_prose_mention_of_a_keyword_is_not_a_signature():
    # "panic" is only a word here; a signature implies a message that follows a
    # colon, so recording one would key a failure group on prose.
    assert normalize_error_signature("this panic is about the pipeline") is None


def test_file_path_with_an_error_shaped_name_is_not_a_signature():
    # ``test_timeout.py`` ends in a keyword, and a path is not an error.
    assert normalize_error_signature("open tests/test_timeout.py and rerun") is None


def test_identical_errors_on_different_hosts_share_one_signature():
    prod = normalize_error_signature("ConnectionRefusedError: connection refused on queue.prod.internal:5432")
    stage = normalize_error_signature("ConnectionRefusedError: connection refused on queue.stage.internal:5432")
    assert prod is not None
    assert prod == stage


def test_camel_case_exception_wins_over_an_incidental_keyword():
    signature = normalize_error_signature("the timeout: see notes\nValueError: bad input")
    assert signature is not None
    assert signature.startswith("valueerror")


def test_bare_keyword_with_a_message_records_a_signature():
    # Rust and Go report errors without a capitalised type name.
    assert normalize_error_signature("panic: index out of range") == "panic: index out of range"


def test_class_prefix_is_escaped_for_like_matching():
    # The retrieval query binds this with ESCAPE '!', so a class name holding a
    # wildcard must match itself and nothing else.
    assert error_signature_like_prefix("value_error%!") == "value!_error!%!!: %"
    assert error_class_signature("valueerror: bad input") == "valueerror"
    assert error_class_signature(None) is None
