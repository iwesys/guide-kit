"""
classify.py — guide-kit structurer: orchestrates homes.yaml + structural signals into
type-index.json (FORMAT.md §2-§4). Deterministic slice only: frontmatter override →
homes.yaml → event-date signal → honest 2.4 default. Quarantine, media preprocessing,
sidecar overrides, and the LLM fallback are separate slices (WP-483 Ф2 checklist), not
implemented here.

CLI:
    python3 classify.py --base <path> [--homes homes.yaml] [--out .structurer/type-index.json]
"""
from __future__ import annotations

import argparse
import json
import logging
import os

from homes import HomeRule, load_homes, match_home
from signals import EVENT_DATE_CONFIDENCE, detect_event_date, read_frontmatter, read_frontmatter_override

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
TEXT_EXTENSIONS = frozenset({".md", ".txt", ".canvas"})
_SKIP_DIRS = frozenset({".structurer", ".git"})
# guide-kit's own config artifacts, not user content — classifying them as
# "needs-extractor" would misreport them in the unplaced-files summary.
_SKIP_FILES = frozenset({"homes.yaml", "guide-kit.config.yaml", "extractors.yaml"})


def classify_file(rel_path: str, abs_path: str, homes: list[HomeRule]) -> dict:
    """One type-index.json entry, per FORMAT.md §3 precedence: non-text extension →
    homes.yaml (non-"auto") → frontmatter override → event-date signal → default 2.4."""
    ext = os.path.splitext(rel_path)[1].lower()
    if ext not in TEXT_EXTENSIONS:
        return {"type": None, "pending": "needs-extractor"}

    home = match_home(rel_path, homes)
    if home is not None and home.type != "auto":
        return {"type": home.type, "mode": "index", "confidence": 1.0, "source": "homes"}

    frontmatter = read_frontmatter(abs_path)

    override = read_frontmatter_override(frontmatter, abs_path)
    if override is not None:
        return {"type": override, "mode": "index", "confidence": 1.0, "source": "frontmatter"}

    if detect_event_date(rel_path, frontmatter):
        return {"type": "2.2", "mode": "index", "confidence": EVENT_DATE_CONFIDENCE, "source": "classifier"}

    return {"type": "2.4", "mode": "index", "confidence": 0.0, "source": "default"}


def walk_and_classify(base_dir: str, homes: list[HomeRule]) -> dict[str, dict]:
    """Walks base_dir, skipping dotfiles/dotdirs and .structurer/.git (a repeat run
    must not classify its own output). Returns the "files" section of type-index.json."""
    files: dict[str, dict] = {}
    for root, dirs, filenames in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        is_base_root = os.path.abspath(root) == os.path.abspath(base_dir)
        for filename in filenames:
            if filename.startswith("."):
                continue
            # Config files are only skipped at the base root — a user's own note
            # happening to share a name (e.g. "homes.yaml" in a subfolder) is content.
            if is_base_root and filename in _SKIP_FILES:
                continue
            abs_path = os.path.join(root, filename)
            rel_path = os.path.relpath(abs_path, base_dir).replace(os.sep, "/")
            files[rel_path] = classify_file(rel_path, abs_path, homes)
    return files


def write_type_index(files: dict[str, dict], out_path: str) -> None:
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": SCHEMA_VERSION, "files": files}, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="guide-kit structurer: classify a note base into type-index.json")
    parser.add_argument("--base", required=True, help="path to the user's note base")
    parser.add_argument("--homes", default="homes.yaml", help="path to homes.yaml (tolerant of absence)")
    parser.add_argument("--out", default=".structurer/type-index.json", help="relative to --base unless absolute")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    homes = load_homes(args.homes)
    files = walk_and_classify(args.base, homes)
    out_path = args.out if os.path.isabs(args.out) else os.path.join(args.base, args.out)
    write_type_index(files, out_path)
    logger.info("classified %d files → %s", len(files), out_path)


if __name__ == "__main__":
    main()
