import json
import os
import time

CACHE_FILE = "data/news_cache.json"
MAX_CACHE_SIZE = 150

def _ensure_dir():
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)

def load_cache() -> list:
    if not os.path.exists(CACHE_FILE):
        return []
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading news cache: {e}")
        return []

def save_cache(data: list):
    _ensure_dir()
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving news cache: {e}")

def add_items(new_items: list):
    """
    Appends new items to the cache. 
    Items should be dicts: {"id": str, "title": str, "url": str, "summary": str, "theme_score": int, "serendipity_score": int, "timestamp": float}
    """
    cache = load_cache()
    
    # Check for duplicates by URL or title
    existing_urls = {item.get("url") for item in cache if item.get("url")}
    existing_titles = {item.get("title") for item in cache if item.get("title")}
    
    added = 0
    for item in new_items:
        if item.get("url") in existing_urls or item.get("title") in existing_titles:
            continue
        item["timestamp"] = time.time()
        cache.append(item)
        added += 1
        
    # Enforce MAX_CACHE_SIZE by keeping the newest ones
    if len(cache) > MAX_CACHE_SIZE:
        cache.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        cache = cache[:MAX_CACHE_SIZE]
        
    save_cache(cache)
    return added

def get_unpushed_items() -> list:
    """Returns items that haven't been pushed yet."""
    cache = load_cache()
    return [item for item in cache if not item.get("pushed")]

def mark_as_pushed(urls: list):
    """Marks specific items as pushed so they aren't included in future digests."""
    cache = load_cache()
    for item in cache:
        if item.get("url") in urls:
            item["pushed"] = True
    save_cache(cache)

def clear_pushed():
    """Removes all items marked as pushed to free up space."""
    cache = load_cache()
    original_len = len(cache)
    cache = [item for item in cache if not item.get("pushed")]
    if len(cache) < original_len:
        save_cache(cache)
    print(f"Cleaned {original_len - len(cache)} pushed items from cache.")

def is_duplicate(url: str, title: str) -> bool:
    """Check if a URL or title already exists in the cache."""
    cache = load_cache()
    for item in cache:
        if item.get("url") == url or item.get("title") == title:
            return True
    return False
