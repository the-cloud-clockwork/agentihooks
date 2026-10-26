import pytest


@pytest.fixture
def tmp_path(memory_path):
    return memory_path
