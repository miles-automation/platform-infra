import importlib.util
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path


def _platform_module():
    path = Path(__file__).parents[1] / "bin" / "platform"
    loader = SourceFileLoader("platform_cli_dotenv", str(path))
    spec = importlib.util.spec_from_loader("platform_cli_dotenv", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dotenv_parser_uses_last_value_like_docker_compose(tmp_path: Path) -> None:
    platform = _platform_module()
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "SPARK_SWARM_API_KEY=stale-first\n"
        "IGNORED=value\n"
        "SPARK_SWARM_API_KEY='active-last'\n"
    )

    result = subprocess.run(
        [sys.executable, "-", str(dotenv)],
        input=platform._DOTENV_LAST_VALUE_PY,
        text=True,
        capture_output=True,
        check=True,
    )

    assert result.stdout == "active-last"
