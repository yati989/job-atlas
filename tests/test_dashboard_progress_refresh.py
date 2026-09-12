import ast
from pathlib import Path


DASHBOARD_PATH = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "main.py"


def _fragment_decorator(function_name: str) -> ast.Call:
    module = ast.parse(DASHBOARD_PATH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    return next(
        decorator
        for decorator in function.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "fragment"
    )


def _function(function_name: str) -> ast.FunctionDef:
    module = ast.parse(DASHBOARD_PATH.read_text(encoding="utf-8"))
    return next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )


def test_both_progress_tables_auto_refresh_inside_scoped_fragments():
    for function_name in (
        "render_live_connector_progress",
        "render_live_decision_run_progress",
    ):
        decorator = _fragment_decorator(function_name)
        run_every = next(
            (keyword.value for keyword in decorator.keywords if keyword.arg == "run_every"),
            None,
        )
        assert isinstance(run_every, ast.Name)
        assert run_every.id == "PROGRESS_REFRESH_INTERVAL_SECONDS"


def test_progress_copy_does_not_claim_refresh_is_manual_only():
    source = DASHBOARD_PATH.read_text(encoding="utf-8")
    assert "refresh manually above" not in source


def test_background_fragment_refresh_does_not_flash_streamlit_status_widget():
    source = DASHBOARD_PATH.read_text(encoding="utf-8")
    assert '[data-testid="stStatusWidgetRunningIcon"]' in source
    assert "visibility: hidden" in source


def test_decision_run_auto_refresh_only_renders_stage_progress():
    """The two-second fragment must not rebuild the expensive drill-down UI."""
    function = _function("render_live_decision_run_progress")
    query_calls = {
        node.func.attr
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "queries"
    }

    assert query_calls == {"decision_run_progress"}


def test_pipeline_health_keeps_stable_decision_run_controls_outside_fragment():
    pipeline_health = _function("render_pipeline_health")
    direct_calls = {
        node.func.id
        for node in pipeline_health.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        for node in [node.value]
    }

    assert "render_decision_run_health" in direct_calls
    assert "render_live_decision_run_progress" not in direct_calls
