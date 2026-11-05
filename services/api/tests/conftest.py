import pytest


@pytest.fixture
def manual_plan_confirmation(monkeypatch):
    """Legacy manual-confirmation flow remains an explicit administrator option."""
    from app.settings import settings_service
    settings = settings_service.get()
    settings.require_plan_confirmation = True
    monkeypatch.setattr(settings_service, 'get', lambda: settings.model_copy(deep=True))
