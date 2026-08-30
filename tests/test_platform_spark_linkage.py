import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path


def _platform_module():
    path = Path(__file__).parents[1] / "bin" / "platform"
    loader = SourceFileLoader("platform_cli", str(path))
    spec = importlib.util.spec_from_loader("platform_cli", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rollout_prefers_explicit_spark_slug() -> None:
    platform = _platform_module()
    cfg = {"projects": {"human-index-v2": {"spark_slug": "human-index"}}}
    assert platform._sparkswarm_project_slug(cfg, "human-index-v2") == "human-index"


def test_human_index_v2_compatibility_alias() -> None:
    platform = _platform_module()
    assert platform._sparkswarm_project_slug({}, "human-index-v2") == "human-index"
