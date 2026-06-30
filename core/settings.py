import json
import os
from typing import Dict, Any

SETTINGS_FILE = "settings.json"

def load_settings() -> Dict[str, Any]:
    if not os.path.exists(SETTINGS_FILE):
        return {}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading settings: {e}")
        return {}

def save_settings(data: Dict[str, Any]):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Error saving settings: {e}")

def get_setting(key: str, default: Any = None) -> Any:
    settings = load_settings()
    return settings.get(key, default)

def set_setting(key: str, value: Any):
    settings = load_settings()
    settings[key] = value
    save_settings(settings)
