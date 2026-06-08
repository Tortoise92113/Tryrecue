import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# Import pattern directly — fast, no side effects
from main import _TITLE_BLOCK, _LOCATION_BLOCK


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
    "Farmstay Host",        # 'farm' inside a word — should NOT match \bfarm\b alone
])
def test_title_block_does_not_block_normal_whv_titles(title):
    assert not _TITLE_BLOCK.search(title), f"Expected {title!r} NOT to be blocked"


def test_location_block_matches_sydney():
    assert _LOCATION_BLOCK.search("Sydney NSW")
    assert _LOCATION_BLOCK.search("sydney")
    assert _LOCATION_BLOCK.search("Greater Sydney")


def test_location_block_does_not_block_other_cities():
    assert not _LOCATION_BLOCK.search("Melbourne")
    assert not _LOCATION_BLOCK.search("Brisbane")
    assert not _LOCATION_BLOCK.search("Cairns")
