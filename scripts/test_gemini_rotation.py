import importlib.util
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hasad_news_bot_fixed", ROOT / "hasad_news_bot_fixed.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def reset_rotation(groups, night_mode, models=("model-1",)):
    MODULE.KEY_GROUPS = groups
    MODULE.MODEL_CASCADE = list(models)
    MODULE.NIGHT_MODE = night_mode
    MODULE._current_group_idx = len(groups) - 1 if night_mode else 0
    MODULE._current_key_idx = len(groups[-1]) - 1 if night_mode else 0
    MODULE._model_stage_idx = 0


def test_period_boundaries():
    assert MODULE.is_night_mode(datetime(2026, 9, 4, 0, 0))
    assert MODULE.is_night_mode(datetime(2026, 9, 4, 13, 59))
    assert not MODULE.is_night_mode(datetime(2026, 9, 4, 14, 0))
    assert not MODULE.is_night_mode(datetime(2026, 9, 4, 23, 59))


def test_day_switches_to_next_group(monkeypatch=None):
    reset_rotation([["a1"], ["b1"]], night_mode=False)
    seen = []

    def fail_once_then_succeed(prompt, schema=None):
        seen.append((MODULE._current_group_idx, MODULE._current_key_idx))
        if len(seen) == 1:
            raise MODULE.DailyQuotaExceeded()
        return "ok"

    original = MODULE.call_gemini
    MODULE.call_gemini = fail_once_then_succeed
    try:
        assert MODULE.call_with_rotation("prompt") == "ok"
    finally:
        MODULE.call_gemini = original
    assert seen == [(0, 0), (1, 0)]


def test_night_switches_to_previous_group():
    reset_rotation([["a1"], ["b1"]], night_mode=True)
    seen = []

    def fail_once_then_succeed(prompt, schema=None):
        seen.append((MODULE._current_group_idx, MODULE._current_key_idx))
        if len(seen) == 1:
            raise MODULE.DailyQuotaExceeded()
        return "ok"

    original = MODULE.call_gemini
    MODULE.call_gemini = fail_once_then_succeed
    try:
        assert MODULE.call_with_rotation("prompt") == "ok"
    finally:
        MODULE.call_gemini = original
    assert seen == [(1, 0), (0, 0)]


if __name__ == "__main__":
    test_period_boundaries()
    test_day_switches_to_next_group()
    test_night_switches_to_previous_group()
    print("Gemini rotation tests passed.")
