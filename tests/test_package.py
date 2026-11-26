import tomllib
from pathlib import Path


def test_phase_one_has_no_ml_extra():
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    assert project["optional-dependencies"]["ml"] == []
