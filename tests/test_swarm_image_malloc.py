from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def test_the_ledger_image_caps_malloc_arenas_so_freed_thread_memory_is_reused():
    final_stage = (ROOT / "Dockerfile").read_text().rsplit("\nFROM ", 1)[1]
    assert "    MALLOC_ARENA_MAX=2 \\\n" in final_stage


def test_the_ledger_image_fixes_malloc_trim_and_mmap_thresholds_so_large_freed_buffers_leave_the_heap():
    final_stage = (ROOT / "Dockerfile").read_text().rsplit("\nFROM ", 1)[1]
    assert "    MALLOC_TRIM_THRESHOLD_=131072 \\\n" in final_stage
    assert "    MALLOC_MMAP_THRESHOLD_=131072 \\\n" in final_stage
