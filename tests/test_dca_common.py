import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dca_common import _num  # noqa: E402


def test_num():
    assert _num(10) == 10.0
    assert _num("10.5") == 10.5
    assert _num("invalid") == 0.0
    assert _num(None) == 0.0
