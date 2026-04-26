#!/usr/bin/env python3
"""Download and resize images for Sigmionary quiz game."""

import time
import requests
from pathlib import Path
from io import BytesIO
from PIL import Image

QUESTIONS_DIR = Path("questions")
TARGET_SIZE = (350, 350)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

SEARCH_QUERIES = {
    # Existing entries (already have images, skip if present)
    "Cap":     "baseball cap hat",
    "Pill":    "medicine pill capsule",
    "Beach":   "tropical beach ocean waves",
    "Mutton":  "mutton lamb chop meat",
    "Cherry":  "red cherry fruit",
    "Conch":   "conch shell spiral",
    "Face":    "cartoon human face smile",
    "Cloud":   "white fluffy cloud blue sky",
    "Laya":    "musical notes rhythm metronome",
    # New entries
    "A":       "letter A bold red alphabet",
    "Three":   "number 3 digit bold",
    "Apple":   "red apple fruit",
    "Lee":     "Bruce Lee kung fu martial arts",
    "Six":     "number 6 digit bold",
    "Chicken": "rooster chicken bird",
    "Nest":    "bird nest eggs",
    "Paddy":   "paddy rice field green",
    "Land":    "green countryside landscape",
    "Cove":    "natural cove inlet sea coast photograph",
    "Lamb":    "lamb sheep young animal farm photograph",
    "Banyan":  "banyan tree aerial roots",
    "River":   "river stream water nature photograph",
    "Naga":    "king cobra snake hood",
    "Mani":    "precious stone ruby crystal gemstone photograph",
    "Pur":     "purple flower lavender field photograph",
    "Tri":     "trident poseidon weapon three prongs photograph",
    "Pura":    "ancient hindu temple india",
    "Mum":     "mummy egypt sarcophagus ancient museum artifact",
    "Bye":     "goodbye farewell person waving",
    "Del":     "keyboard backspace delete key",
    "Hi":      "greeting wave hand hello",
    "Go":      "go weiqi board game stones",
    "Coal":    "coal black rock mineral pile photograph",
    "Cat":     "domestic cat sitting photograph",
}

# (category, item, [(prefix, term), ...])
ENTRIES = [
    ("Kerala", "Athirappilly", [("1", "A"), ("2", "Three"), ("3", "Apple"), ("4", "Lee")]),
    ("Kerala", "Munnar",       [("1", "Three"), ("2", "Six")]),
    ("Kerala", "Kozhikode",    [("1", "Chicken"), ("2", "Nest")]),
    ("Kerala", "Wayanad",      [("1", "Paddy"), ("2", "Land")]),
    ("Kerala", "Kovalam",      [("1", "Cove"), ("2", "Lamb")]),
    ("Kerala", "Alappuzha",    [("1", "Banyan"), ("2", "River")]),
    ("India",  "Nagaland",     [("1", "Naga"), ("2", "Land")]),
    ("India",  "Manipur",      [("1", "Mani"), ("2", "Pur")]),
    ("India",  "Tripura",      [("1", "Tri"), ("2", "Pura")]),
    ("India",  "Goa",          [("1", "Go"), ("2", "A")]),
    ("India",  "Mumbai",       [("1", "Mum"), ("2", "Bye")]),
    ("India",  "Delhi",        [("1", "Del"), ("2", "Hi")]),
    ("India",  "Kolkata",      [("1", "Coal"), ("2", "Cat"), ("3", "A")]),
]


def download_and_save(url: str, save_path: Path) -> bool:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return False
        img = Image.open(BytesIO(resp.content)).convert("RGB")
        img.thumbnail(TARGET_SIZE, Image.LANCZOS)
        img.save(save_path, "JPEG", quality=85, optimize=True)
        return True
    except Exception as e:
        print(f"    error: {e}")
        return False


WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"


def _wikimedia_image_urls(query: str, limit: int = 15):
    """Yield direct image URLs from Wikimedia Commons for a query."""
    search_params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srnamespace": "6",
        "format": "json",
        "srlimit": limit,
    }
    try:
        resp = requests.get(WIKIMEDIA_API, params=search_params, headers=HEADERS, timeout=10)
        titles = [r["title"] for r in resp.json().get("query", {}).get("search", [])]
    except Exception as e:
        print(f"    wikimedia search error: {e}")
        return

    SKIP_EXTS = {".svg", ".pdf", ".ogg", ".ogv", ".gif", ".webm", ".tiff", ".tif"}
    for title in titles:
        ext = Path(title).suffix.lower()
        if ext in SKIP_EXTS:
            continue
        info_params = {
            "action": "query",
            "titles": title,
            "prop": "imageinfo",
            "iiprop": "url|mediatype",
            "iiurlwidth": 400,
            "format": "json",
        }
        try:
            r = requests.get(WIKIMEDIA_API, params=info_params, headers=HEADERS, timeout=10)
            pages = r.json().get("query", {}).get("pages", {})
            for page in pages.values():
                info = page.get("imageinfo", [{}])[0]
                url = info.get("thumburl") or info.get("url", "")
                if url and not url.lower().endswith(".svg"):
                    yield url
        except Exception:
            continue
        time.sleep(0.2)


def search_and_download(term: str, save_path: Path) -> bool:
    query = SEARCH_QUERIES.get(term, term)
    for url in _wikimedia_image_urls(query):
        if download_and_save(url, save_path):
            return True
    return False


def main():
    downloaded, failed = [], []

    for category, item, images in ENTRIES:
        folder = QUESTIONS_DIR / category / item
        folder.mkdir(parents=True, exist_ok=True)
        print(f"\n{category}/{item}")

        for prefix, term in images:
            existing = list(folder.glob(f"{prefix}-{term}.*"))
            if existing:
                print(f"  ✓ {prefix}-{term} (exists)")
                downloaded.append(f"{category}/{item}/{term}")
                continue

            save_path = folder / f"{prefix}-{term}.jpg"
            print(f"  ↓ {prefix}-{term}  [{SEARCH_QUERIES.get(term, term)}]")

            if search_and_download(term, save_path):
                print(f"  ✓ saved → {save_path}")
                downloaded.append(f"{category}/{item}/{term}")
            else:
                print(f"  ✗ FAILED: {category}/{item}/{prefix}-{term}")
                failed.append(f"{category}/{item}/{prefix}-{term}")

            time.sleep(1.2)

    print(f"\n{'='*50}")
    print(f"Done — {len(downloaded)} downloaded, {len(failed)} failed")
    if failed:
        print("Failed:")
        for f in failed:
            print(f"  - {f}")


if __name__ == "__main__":
    main()
