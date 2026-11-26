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


def test_dotted_relation_names_keep_distinct_signatures():
    # "database.schema" / "service.schema" are qualified identifiers, not
    # hosts: collapsing both to <host> merged two unrelated failures.
    database = normalize_error_signature("ProgrammingError: relation database.schema does not exist")
    service = normalize_error_signature("ProgrammingError: relation service.schema does not exist")
    assert database is not None
    assert service is not None
    assert database != service


def test_host_shaped_identifiers_still_normalize_away():
    from_url = normalize_error_signature("HTTPError: POST https://api.prod.internal/v1/run failed")
    from_userinfo = normalize_error_signature("HTTPError: POST https://ops@api.stage.internal/v1/run failed")
    assert from_url is not None
    assert from_userinfo is not None
    assert "<host>" in from_url
    assert "<host>" in from_userinfo


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


def test_a_header_without_a_detail_does_not_swallow_the_next_line():
    # "timeout:" carries no detail, so the exception on the next line is the
    # failure that must key the signature.
    assert normalize_error_signature("timeout:\nValueError: bad input") == "valueerror: bad input"
    assert normalize_error_signature("timeout:") == "timeout"


def test_quoted_host_names_normalise_to_one_signature():
    # Some drivers quote the host, so the same unreachable database arrived as
    # two failure groups depending on which driver logged it.
    signatures = {
        normalize_error_signature(
            "OperationalError: could not translate host name "
            f"{quote}db1.prod.internal{quote} to address: Name or service not known"
        )
        for quote in ('"', "'")
    }
    assert None not in signatures
    assert len(signatures) == 1


def test_quoted_single_label_host_normalises_like_a_dotted_host() -> None:
    dotted = normalize_error_signature(
        'OperationalError: could not translate host name "db.prod.internal" to address: Name or service not known'
    )
    internal = normalize_error_signature(
        'OperationalError: could not translate host name "db" to address: Name or service not known'
    )

    assert dotted
    assert "prod.internal" not in dotted
    assert internal == dotted
