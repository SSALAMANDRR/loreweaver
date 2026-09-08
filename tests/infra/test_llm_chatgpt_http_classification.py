from infra.llm_chatgpt import ProviderResponseError, _status_error_payload


def test_bare_http_400_is_request_error_not_content_error():
    error = ProviderResponseError(
        "http.error",
        _status_error_payload(400, None),
        code="subscription_http_error",
    )

    assert error.category == "request"


def test_provider_invalid_prompt_code_still_wins_over_generic_http_400():
    error = ProviderResponseError(
        "http.error",
        _status_error_payload(
            400,
            {"error": {"type": "invalid_request_error", "code": "invalid_prompt"}},
        ),
        code="subscription_http_error",
    )

    assert error.category == "content"


def test_http_413_remains_input_too_long_content_error():
    error = ProviderResponseError(
        "http.error",
        _status_error_payload(413, None),
        code="subscription_http_error",
    )

    assert error.category == "content"
