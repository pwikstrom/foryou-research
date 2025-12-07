



import time
import logging
from pathlib import Path
from typing import Set

# === CONFIG ===
WATCH_DIR = Path('/Users/<user>/Desktop/test_folder')   # folder with final files
PATTERN_SUFFIX = ".txt"               # final suffix to react to
PATTERN_PREFIX = "test"               # prefix to react to
LOG_FILE = Path("/Users/<user>/Desktop/test_folder/watcher.log")
POLL_INTERVAL = 60.0                  # seconds between scans


def setup_logger() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def load_processed_files() -> Set[Path]:
    """
    Read log and return set of fully processed file paths.
    """
    processed: Set[Path] = set()
    if not LOG_FILE.exists():
        return processed

    with LOG_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("PROCESSED:"):
                path_str = line.split("PROCESSED:", 1)[1].strip()
                if path_str:
                    processed.add(Path(path_str))
    return processed


def mark_processed(path: Path) -> None:
    """
    Mark file as processed in log.
    """
    logging.info("Completed processing %s", path)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"PROCESSED:{path}\n")


def find_candidate_files() -> Set[Path]:
    """
    Return all files in WATCH_DIR with the final suffix.
    """
    return {
        p.resolve()
        for p in WATCH_DIR.iterdir()
        if p.is_file() and p.name.startswith(PATTERN_PREFIX) and p.name.endswith(PATTERN_SUFFIX)
    }


def handle_new_file(path: Path) -> None:
    """
    Your long running processing function.
    """
    logging.info("Starting heavy work on %s", path)
    # Replace this with the real job
    for i in range(5):
        logging.info("Working on %s step %d/5", path, i + 1)
        time.sleep(1)
    logging.info("Heavy work finished for %s", path)


def main() -> None:
    setup_logger()
    logging.info("Starting watcher. dir=%s | prefix=%s | suffix=%s", WATCH_DIR, PATTERN_PREFIX, PATTERN_SUFFIX)

    processed = load_processed_files()
    logging.info("Loaded %d processed files from log", len(processed))

    try:
        while True:
            # 1) Scan for candidates
            candidates = find_candidate_files()

            # 2) Choose files we haven't processed yet
            new_files = sorted(p for p in candidates if p not in processed)

            if new_files:
                logging.info("Found %d unprocessed file(s)", len(new_files))

            # 3) Process them one by one
            for path in new_files:
                try:
                    handle_new_file(path)
                    mark_processed(path)
                    processed.add(path)
                except Exception:
                    logging.exception("Error while processing %s", path)

            # 4) Sleep before next scan
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received. Shutting down.")


if __name__ == "__main__":
    main()
