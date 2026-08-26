"""
The whatsapp trust secret, and why its configuration has to be observable.

The Edge traffic controller sends X-Internal-Secret from Supabase's
BACKEND_INTERNAL_SECRET; this backend compared it against Railway's
INTERNAL_API_SECRET. Setting the Edge function's name on Railway did nothing, because
the backend never read it.

That would be a small naming bug if it failed loudly. It does not:
verify_internal_secret() returns True when no secret is configured, so the Edge function
gets 200 and the log still says "Auth verified". A completely unauthenticated trust path
is indistinguishable from a working one from the outside - which is why the state is
reported on /health rather than left to a warning line in the logs.
"""
import os
import sys

os.environ.setdefault("OPENAI_API_KEY", "test-key")

from config.settings import Settings, internal_secret_status  # noqa: E402


# config/__init__.py binds the name "settings" to the instance, which shadows the
# submodule - so `import config.settings as m` hands back the instance, not the module.
_MODULE = sys.modules["config.settings"]


def _settings_with(**env) -> Settings:
    """Build Settings with the secret overridden.

    The .env file is left in place: it supplies the required fields (API keys, Supabase
    URL) that have no defaults, and the explicit kwarg wins over it for the secret.
    """
    return Settings(**env)


# ── Either name is accepted ──────────────────────────────────────────────────

def test_primary_name_is_read():
    assert _settings_with(INTERNAL_API_SECRET="abc").internal_api_secret == "abc"


def test_edge_functions_name_is_also_read():
    """The name the Edge side uses must work when it is the one that got set."""
    assert _settings_with(BACKEND_INTERNAL_SECRET="xyz").internal_api_secret == "xyz"


def test_primary_wins_when_both_are_set():
    s = _settings_with(INTERNAL_API_SECRET="abc", BACKEND_INTERNAL_SECRET="xyz")
    assert s.internal_api_secret == "abc"


# ── The state is observable ──────────────────────────────────────────────────

def _status(monkey_env: dict) -> dict:
    """Run internal_secret_status() against a controlled environment."""
    module = _MODULE

    saved_env = {k: os.environ.get(k) for k in ("INTERNAL_API_SECRET", "BACKEND_INTERNAL_SECRET")}
    saved_settings = module.settings
    try:
        for key in saved_env:
            os.environ.pop(key, None)
        os.environ.update({k: v for k, v in monkey_env.items() if v is not None})
        module.settings = Settings(**{k: v for k, v in monkey_env.items()} or {"INTERNAL_API_SECRET": ""})
        return internal_secret_status()
    finally:
        module.settings = saved_settings
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_no_secret_reports_unauthenticated():
    """The case that fails open, and therefore the one that must be visible."""
    status = _status({})
    assert status["state"] == "unauthenticated"
    assert status["using"] is None


def test_conflicting_values_are_reported():
    """Both names set to different values: only one takes effect, so say which."""
    status = _status({"INTERNAL_API_SECRET": "abc", "BACKEND_INTERNAL_SECRET": "xyz"})
    assert status["state"] == "conflict"
    assert status["using"] == "INTERNAL_API_SECRET"
    assert set(status["configured_names"]) == {"INTERNAL_API_SECRET", "BACKEND_INTERNAL_SECRET"}


def test_matching_values_are_not_a_conflict():
    status = _status({"INTERNAL_API_SECRET": "abc", "BACKEND_INTERNAL_SECRET": "abc"})
    assert status["state"] == "configured"


def test_either_name_alone_is_configured():
    for name in ("INTERNAL_API_SECRET", "BACKEND_INTERNAL_SECRET"):
        status = _status({name: "abc"})
        assert status["state"] == "configured", name
        assert status["using"] == name, name


def test_status_never_leaks_the_secret():
    """This is served publicly on /health; it may name variables, never values."""
    status = _status({"INTERNAL_API_SECRET": "s3cr3t-value", "BACKEND_INTERNAL_SECRET": "other"})
    assert "s3cr3t-value" not in str(status)
    assert "other" not in str(status)


# ── The check itself ─────────────────────────────────────────────────────────

def test_wrong_secret_is_rejected():
    # jwt_auth does `from config.settings import settings`, binding the instance under
    # its own name at import time, so the patch has to land there rather than on the
    # config module. In production both names point at the same object.
    import services.jwt_auth as jwt_auth

    saved = jwt_auth.settings
    try:
        jwt_auth.settings = Settings(INTERNAL_API_SECRET="abc")
        assert jwt_auth.verify_internal_secret("abc") is True
        assert jwt_auth.verify_internal_secret("wrong") is False
        assert jwt_auth.verify_internal_secret("") is False
        assert jwt_auth.verify_internal_secret(None) is False
    finally:
        jwt_auth.settings = saved


def test_unset_secret_fails_open():
    """Documents the behaviour that made the naming bug invisible.

    This is deliberate - it lets the backend deploy before the secret is provisioned -
    but it is exactly why the state has to be readable from /health.
    """
    import services.jwt_auth as jwt_auth

    saved = jwt_auth.settings
    try:
        jwt_auth.settings = Settings(INTERNAL_API_SECRET="")
        assert jwt_auth.verify_internal_secret(None) is True
        assert jwt_auth.verify_internal_secret("anything at all") is True
    finally:
        jwt_auth.settings = saved
