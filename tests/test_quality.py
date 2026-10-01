"""音质阶梯与标签（core.quality）。"""

from astrbot_plugin_music_hub.core.quality import ladder_for, quality_label, trial_suffix


def test_ladder_auto_returns_full_chain():
    for src in ("ncm", "kg", "qq"):
        ladder = ladder_for(src, "auto")
        assert ladder, f"{src} 阶梯不应为空"
        assert all(isinstance(q, str) for q in ladder)


def test_ladder_respects_preferred_cap():
    ladder = ladder_for("kg", "128")
    assert "128" in ladder
    # 指定 128 时不应再出现更高档位排在 128 之前
    assert ladder[0] == "128" or "128" not in ladder


def test_quality_label_known_and_fallback():
    assert quality_label("ncm", "auto")
    assert quality_label("ncm", "__unknown__") == "__unknown__"


def test_trial_suffix_marks_trial():
    assert trial_suffix({"trial": True})
    assert not trial_suffix({"trial": False})
