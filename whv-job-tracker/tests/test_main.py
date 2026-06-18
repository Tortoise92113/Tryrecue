import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from filters import compile_blocklist, pre_filter

# Same keywords as config.yml's filters.title_block
_TITLE_KEYWORDS = [
    "senior", "chef", "farmer", "farming",
    "farm manager", "farm hand", "farm worker", "farm assistant",
    "agricultural", "agriculture",
]
_TITLE_BLOCK = compile_blocklist(_TITLE_KEYWORDS)


@pytest.mark.parametrize("title", [
    "Senior Software Developer",
    "Senior Barista",
    "Head Chef",
    "sous chef",
    "Chef de Partie",
    "Farm Manager",
    "Farm Hand",
    "Farm Worker",
    "Farm Assistant",
    "Agricultural Technician",
    "Agriculture Officer",
    "Farming Assistant",
    "Farmer Wanted",
])
def test_title_block_matches_blocked_titles(title):
    assert _TITLE_BLOCK.search(title), f"Expected {title!r} to be blocked"


@pytest.mark.parametrize("title", [
    "Barista",
    "Kitchen Hand",
    "Fruit Picker",
    "Hotel Receptionist",
    "Warehouse Worker",
    "Hospitality Staff",
    "Retail Assistant",
    "Office Administrator",
    "Farmstay Host",        # 'farm' inside a word — should NOT match \bfarm...\b
])
def test_title_block_does_not_block_normal_whv_titles(title):
    assert not _TITLE_BLOCK.search(title), f"Expected {title!r} NOT to be blocked"


def test_compile_blocklist_empty_returns_none():
    assert compile_blocklist([]) is None
    assert compile_blocklist(None) is None
    assert compile_blocklist(["", "  "]) is None


def test_pre_filter_uses_config():
    jobs = [
        {"title": "Barista", "city": "Cairns"},
        {"title": "Head Chef", "city": "Cairns"},
        {"title": "Waiter", "city": "Sydney"},
    ]
    config = {"filters": {"title_block": ["chef"], "location_block": ["Sydney"]}}
    kept, dropped = pre_filter(jobs, config)
    assert dropped == 2
    assert [j["title"] for j in kept] == ["Barista"]


def test_pre_filter_empty_blocklists_keep_everything():
    jobs = [{"title": "Head Chef"}, {"title": "Farmer"}]
    config = {"filters": {"title_block": [], "location_block": []}}
    kept, dropped = pre_filter(jobs, config)
    assert dropped == 0
    assert len(kept) == 2
