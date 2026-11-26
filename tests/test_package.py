import tomllib
from pathlib import Path


def test_ml_runtime_is_available_only_as_an_extra():
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    runtime = {"onnxruntime", "tokenizers", "usearch", "numkong"}
    for package in runtime:
        assert any(dep.startswith(package) for dep in project["optional-dependencies"]["ml"])
        assert all(not dep.startswith(package) for dep in project["dependencies"])
        assert all(
            not dep.startswith(package) for dep in project["optional-dependencies"]["worker"]
        )
