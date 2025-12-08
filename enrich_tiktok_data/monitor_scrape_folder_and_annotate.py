


import sys
from pathlib import Path
from typing import Set
import time
import logging

# === SETUP PATH ===
# look for the folder that contains the __proj__.py file, which is the root folder for the project structure
# We use __file__ instead of getcwd() because this is a script, not a notebook
current_dir = Path(__file__).resolve().parent
while not (current_dir / "__proj__.py").exists():
    if current_dir == current_dir.parent:
        raise FileNotFoundError("Could not find __proj__.py in any parent directory")
    current_dir = current_dir.parent
sys.path.append(str(current_dir))

import fyp.fyp_main as fyp

# === CONFIG ===
WATCH_DIR = Path(fyp.cf['paths']['scrape'])                  # folder with the files to watch
PATTERN_SUFFIX = ".pkl"                                      # final suffix to react to
PATTERN_PREFIX = "scrape_metadata_"                          # prefix to react to
LOG_FILE = Path(fyp.cf['paths']['scrape'] + "/watcher.log")  # log file
POLL_INTERVAL = 10.0                                         # seconds between scans
PROCESS_STARTUP_BACKLOG = False                               # should the watcher process files that are already in the folder?


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
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("google.api_core").setLevel(logging.WARNING)
    logging.getLogger("google.auth").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("grpc").setLevel(logging.WARNING)
    logging.getLogger("absl").setLevel(logging.WARNING)


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
    from os.path import basename
    #logging.info("Completed processing %s", basename(path))
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
    from os.path import basename
    from fyp.machine_annotation import annotate_from_scrape_metadata_file
    """
    Doing the hard work.
    """
    logging.info("Starting annotation of %s", basename(path))
    annotate_from_scrape_metadata_file(path, verbose = False)
    logging.info("Annotation finished for %s", basename(path))


def main() -> None:




    setup_logger()
    logging.info("Starting watcher. dir=%s | prefix=%s | suffix=%s | startup_backlog=%s", 
    WATCH_DIR, 
    PATTERN_PREFIX, 
    PATTERN_SUFFIX,
    PROCESS_STARTUP_BACKLOG)

    from os import environ
    if environ.get("FYP_TESTING") and environ.get("FYP_TESTING") == "true":
        logging.info("Test mode enabled")   

    # 1) Load already processed files from the log
    processed = load_processed_files()
    logging.info("Loaded %d processed files from log", len(processed))
    existing_files = find_candidate_files()
    logging.info("Found %d existing file(s) in watch dir", len(existing_files))

    if PROCESS_STARTUP_BACKLOG:
        # Process files that are not yet marked as processed
        backlog = sorted(p for p in existing_files if p not in processed)
        logging.info(
            "Startup backlog mode ON: %d existing file(s) to process", len(backlog)
        )

        for path in backlog:
            try:
                handle_new_file(path)
                mark_processed(path)
                processed.add(path)
            except Exception:
                logging.exception("Error processing backlog file %s", path)
    else:
        # Treat all existing files as already processed for this run
        logging.info(
            "Startup backlog mode OFF: skipping %d existing file(s)", len(existing_files)
        )
        for path in existing_files:
            if path not in processed:
                # Only update the in-memory set; do not touch the log
                processed.add(path)

    # 3) Main loop: watch for new files forever
    try:
        while True:
            candidates = find_candidate_files()
            new_files = sorted(p for p in candidates if p not in processed)

            if new_files:
                logging.info("\nFound %d new unprocessed file(s)", len(new_files))

            for ii, path in enumerate(new_files):
                try:
                    handle_new_file(path)
                    mark_processed(path)
                    processed.add(path)
                except Exception:
                    logging.exception("Error while processing %s", path)
                
                if ii == len(new_files) - 1:
                    logging.info("Done - Back to waiting for a new file to appear in the folder...\n")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received. Shutting down.")




if __name__ == "__main__":
    main()
