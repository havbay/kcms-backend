from kcms.openapi_export import ARTIFACT_PATH, render_artifact


def test_committed_artifact_matches_application_byte_for_byte():
    """Contract drift between backend and frontend must fail deterministically."""
    assert ARTIFACT_PATH.exists(), "run: uv run python -m kcms.openapi_export"
    assert ARTIFACT_PATH.read_text(encoding="utf-8") == render_artifact()


def test_health_operation_is_named_for_client_generation():
    import json

    schema = json.loads(render_artifact())
    assert schema["openapi"].startswith("3.1")
    assert schema["paths"]["/api/v1/health"]["get"]["operationId"] == "getHealth"
    assert set(schema["paths"]["/api/v1/health"]["get"]["responses"]) == {"200", "503"}


def test_public_signup_is_not_advertised_in_the_contract():
    import json

    schema = json.loads(render_artifact())
    assert "/api/v1/auth/signup" not in schema["paths"]
