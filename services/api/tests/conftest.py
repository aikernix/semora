import pytest


@pytest.fixture
def manual_plan_confirmation(monkeypatch):
    """Legacy manual-confirmation flow remains an explicit administrator option."""
    from app.settings import settings_service
    settings = settings_service.get()
    settings.require_plan_confirmation = True
    monkeypatch.setattr(settings_service, 'get', lambda: settings.model_copy(deep=True))


LEGACY_PROTOCOL_MODULES = {
    'test_agent_context','test_agent_convergence','test_agent_runtime','test_continuation',
    'test_plan_candidates','test_prepared_context','test_review_loop','test_sequential_wiki',
    'test_source_execution','test_staged_protocol','test_task_handoff','test_task_memory',
}


@pytest.fixture(autouse=True)
def explicit_legacy_compatibility_modules(monkeypatch, request):
    """Only named legacy contract suites opt out of the production default.

    New test modules run with the production default unless they explicitly
    exercise the compatibility adapter. This is not a suite-wide feature off.
    """
    if request.module.__name__.rsplit('.',1)[-1] in LEGACY_PROTOCOL_MODULES:
        monkeypatch.setenv('AGENT_QUERY_INTENT','false')
