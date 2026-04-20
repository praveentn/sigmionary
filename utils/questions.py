import csv
import logging
from pathlib import Path

from rapidfuzz import fuzz

log = logging.getLogger("sigmionary")

QUESTIONS_DIR = Path(__file__).parent.parent / "questions"
DATA_CSV = QUESTIONS_DIR / "data.csv"

_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _find_item_folder(category: str, item: str) -> Path | None:
    """
    Return the folder under questions/<category>/ whose name best fuzzy-matches
    `item`. Returns None if nothing scores above 70.
    """
    cat_dir = QUESTIONS_DIR / category
    if not cat_dir.is_dir():
        log.warning("Category folder not found: %s", cat_dir)
        return None

    best_score, best_folder = 0, None
    for entry in cat_dir.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        score = fuzz.ratio(entry.name.lower(), item.lower())
        if score > best_score:
            best_score, best_folder = score, entry

    if best_score < 70:
        log.warning(
            "No folder matched item '%s' in category '%s' (best score=%d)",
            item, category, best_score,
        )
        return None
    return best_folder


def _sorted_images(folder: Path) -> list[Path]:
    """Return image files sorted by their leading numeric prefix (1-foo.jpg, 2-bar.png…)."""
    images = []
    for f in folder.iterdir():
        if f.suffix.lower() in _IMG_EXTS and not f.name.startswith("."):
            try:
                prefix = int(f.stem.split("-")[0])
                images.append((prefix, f))
            except (ValueError, IndexError):
                log.warning("Skipping image with unexpected name format: %s", f)
    images.sort(key=lambda t: t[0])
    return [f for _, f in images]


def load_questions() -> list[dict]:
    """
    Parse data.csv and return a list of question dicts:
    {id, category, subcategory, item, images: [Path, ...]}

    Questions without a matching folder or with no images are skipped.
    """
    if not DATA_CSV.exists():
        log.error("data.csv not found at %s", DATA_CSV)
        return []

    questions = []
    with open(DATA_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            num = (row.get("#") or "").strip()
            category = (row.get("Category") or "").strip()
            subcategory = (row.get("Sub-category") or "").strip()
            item = (row.get("Item") or "").strip()

            if not item or not category:
                continue

            folder = _find_item_folder(category, item)
            if folder is None:
                continue

            images = _sorted_images(folder)
            if not images:
                log.warning("No images found for %s / %s", category, item)
                continue

            questions.append(
                {
                    "id": num,
                    "category": category,
                    "subcategory": subcategory,
                    "item": item,
                    "images": images,
                }
            )

    log.info("Loaded %d question(s) from data.csv", len(questions))
    return questions
