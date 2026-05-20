import json
import os
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class ValidatorState:
    last_processed_id: Optional[str] = None
    docs_compared: int = 0
    diffs_found: int = 0
    missing_in_target: int = 0
    missing_in_source: int = 0


def load_state(path: str) -> ValidatorState:
    if os.path.exists(path):
        with open(path) as f:
            return ValidatorState(**json.load(f))
    return ValidatorState()


def save_state(path: str, state: ValidatorState) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(state), f)
    os.replace(tmp, path)
