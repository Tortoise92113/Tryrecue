from pathlib import Path

import yaml


def load_config() -> dict:
    path = Path(__file__).parent / "config.yml"
    with open(path) as f:
        return yaml.safe_load(f)
