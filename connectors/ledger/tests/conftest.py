import os
import pathlib
import shutil
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
REPO = HERE.parents[2]


@pytest.fixture(scope="session")
def warrant_ids():
    path = os.environ.get("WARRANT_IDS") or shutil.which("warrant-ids")
    if not path:
        for profile in ("release", "debug"):
            candidate = REPO / "target" / profile / "warrant-ids"
            if candidate.exists():
                path = str(candidate)
                break
    if not path:
        pytest.skip("warrant-ids not built: cargo build -p warrant-ids --release")
    return path


@pytest.fixture(scope="session")
def vectors():
    import json
    return json.loads((REPO / "ids" / "tests" / "data" / "vectors.json").read_text())


@pytest.fixture(scope="session")
def invoice_xml():
    return str(REPO / "ids" / "tests" / "data" / "invoice.xml")
