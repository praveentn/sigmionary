import os
from pathlib import Path

# Mount path for Railway Volume (override via IMAGES_PATH env var for local dev)
IMAGES_PATH = Path(os.getenv("IMAGES_PATH", "/sigmionary/images"))
