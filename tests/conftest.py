"""Cross-process source tests opt into an explicitly built maintained fork."""
import os
from pathlib import Path

import pytest


@pytest.fixture
def funes_bin():
    binary = os.environ.get('FUNES_TEST_BIN')
    if not binary:
        pytest.skip('set FUNES_TEST_BIN to the actual compatible fork executable')
    assert Path(binary).is_absolute() and os.access(binary, os.X_OK)
    return binary
