"""model/projects: a bare name resolves here or in exactly one project; a
name defined twice is refused, a path is a path, the listing labels a
project's chain with its project."""
import os
import tempfile
from pathlib import Path

import pytest

from model import projects as P


@pytest.fixture
def tree(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "chains").mkdir(); (root / "platforms").mkdir()
        (root / "chains" / "own.json").write_text("{}")
        (root / "chains" / "both.json").write_text("{}")
        for name in ("acme", "beta"):
            (root / "projects" / name / "chains").mkdir(parents=True)
            (root / "projects" / name / "platforms").mkdir(parents=True)
        (root / "projects" / "acme" / "chains" / "rig.json").write_text("{}")
        (root / "projects" / "acme" / "platforms" / "desk.yaml").write_text("{}")
        (root / "projects" / "beta" / "chains" / "both.json").write_text("{}")
        monkeypatch.setattr(P, "ROOT", root)
        monkeypatch.setattr(P, "PROJECTS", root / "projects")
        yield root


def test_own_chain(tree):
    assert P.find_chain("own") == tree / "chains" / "own.json"


def test_project_chain(tree):
    assert P.find_chain("rig") == tree / "projects" / "acme" / "chains" / "rig.json"


def test_project_platform(tree):
    assert P.find_platform("desk") == tree / "projects" / "acme" / "platforms" / "desk.yaml"


def test_ambiguous_is_refused(tree):
    with pytest.raises(P.Ambiguous) as e:
        P.find_chain("both")
    assert "chains/both.json" in str(e.value) and "projects/beta/chains/both.json" in str(e.value)


def test_missing_names_where_it_looked(tree):
    with pytest.raises(FileNotFoundError) as e:
        P.find_chain("nope")
    assert "projects/acme/chains" in str(e.value)


def test_a_path_is_a_path(tree):
    p = tree / "chains" / "own.json"
    assert P.find_chain(str(p)) == p


def test_listing_labels_projects(tree):
    labels = [l for l, _ in P.list_chains()]
    assert labels == ["both", "own", "acme/rig", "beta/both"]
