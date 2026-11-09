import pytest

from app.settings import SettingsRepository


def test_settings_policy_is_versioned_and_hashed():
    repository = SettingsRepository(dsn=None)
    original = repository.get()
    changed = original.model_copy(deep=True)
    changed.query_timeout_seconds = 20

    saved = repository.update(changed, "security.admin")

    assert saved.version == original.version + 1
    assert saved.policy_version != original.policy_version
    assert saved.policy_version.startswith(f"query-policy-v{saved.version}-")
    assert saved.updated_by == "security.admin"


def test_settings_reject_preview_above_result_limit():
    repository = SettingsRepository(dsn=None)
    changed = repository.get()
    changed.max_result_rows = 50
    changed.preview_rows = 100

    with pytest.raises(ValueError, match="预览行数"):
        repository.update(changed, "security.admin")
