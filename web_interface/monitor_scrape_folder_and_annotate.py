

def main() -> None:

    import sys
    import os
    from pathlib import Path
    from typing import Set
    import time



    # === SETUP PATH ===
    # look for the folder that contains the __proj__.py file, which is the root folder for the project structure
    # We use __file__ instead of getcwd() because this is a script, not a notebook
    current_dir = Path(__file__).resolve().parent
    while not (current_dir / "__proj__.py").exists():
        if current_dir == current_dir.parent:
            raise FileNotFoundError("Could not find __proj__.py in any parent directory")
        current_dir = current_dir.parent
    sys.path.append(str(current_dir))

    from fyp.fyp_main import init_config, connect_to_google
    import fyp.data_io as data_io

    cf = init_config()
    cf = connect_to_google(cf)


    # === CONFIG ===
    TRACE_STORAGE_LOCATION = "scrape"
    # Even if we use GCS, we keep the log file local in the mapped directory
    WATCH_DIR_LOCAL = Path(cf['paths'][TRACE_STORAGE_LOCATION]) 
    PATTERN_SUFFIX = cf['misc']['file_format']                                      # final suffix to react to
    PATTERN_PREFIX = "scrape_metadata_"                          # prefix to react to
    STATE_FILE = "watcher_state.json"                            # JSON file in GCS to track state
    POLL_INTERVAL = 10.0                                         # seconds between scans
    PROCESS_STARTUP_BACKLOG = False                               # Default to True so we don't miss files after reboot


    # Ensure local directory exists for the log file (still useful for debug or legacy checks)
    if not WATCH_DIR_LOCAL.exists():
        os.makedirs(WATCH_DIR_LOCAL, exist_ok=True)


    def save_processed_state(processed: Set[str]) -> None:
        """
        Save the set of processed filenames to GCS/Local JSON state file.
        """
        try:
            # Save as a list
            data_io.save_json(cf, list(processed), TRACE_STORAGE_LOCATION, STATE_FILE, verbose=False)
        except Exception as e:
            print(f"Error saving watcher state: {e}")


    def load_processed_files() -> Set[str]:
        """
        Load processed filenames from GCS state file.
        Includes migration logic for legacy local watcher.log.
        """
        processed: Set[str] = set()
        
        # 1. Try loading authoritative state from GCS/storage
        try:
            state_data = data_io.load_json(cf, TRACE_STORAGE_LOCATION, STATE_FILE, verbose=False)
            if state_data and isinstance(state_data, list):
                print(f"Loaded persistent state with {len(state_data)} records.")
                return set(state_data)
        except Exception:
            pass # File likely doesn't exist yet

        # 2. Migration: Check for legacy local watcher.log
        legacy_log = WATCH_DIR_LOCAL / "watcher.log"
        if legacy_log.exists():
            print("Found legacy local watcher.log. Migrating state to storage...")
            try:
                with legacy_log.open("r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("PROCESSED:"):
                            path_str = line.split("PROCESSED:", 1)[1].strip()
                            if path_str:
                                processed.add(os.path.basename(path_str))
                
                # Save immediately to establish the new state file
                if processed:
                    save_processed_state(processed)
                    print(f"Migrated {len(processed)} records to {STATE_FILE}")
            except Exception as e:
                print(f"Error migrating legacy log: {e}")
                
        return processed


    def mark_processed(filename: str, processed_set: Set[str]) -> None:
        """
        Mark file as processed in memory and persist state.
        """
        print(f"Completed processing {filename}")
        processed_set.add(filename)
        save_processed_state(processed_set)
        
        # Optional: Keep appending to local log for redundant debugging?
        # Maybe unnecessary now, removing to avoid confusion.


    def find_candidate_files() -> Set[str]:
        """
        Return all files in WATCH_DIR (Local or GCS) with the final suffix.
        Returns filenames (strings).
        """
        # data_io.listdir handles GCS transparently
        try:
            files = data_io.listdir(cf, TRACE_STORAGE_LOCATION, verbose=False)
        except Exception as e:
            print(f"Error listing files: {e}")
            return set()

        return {
            f for f in files
            if f.startswith(PATTERN_PREFIX) and f.endswith(PATTERN_SUFFIX)
        }


    def handle_new_file(filename: str) -> None:
        from fyp.machine_annotation import annotate_from_scrape_metadata_file
        """
        Doing the hard work.
        """
        print(f"Starting annotation of {filename}")
        # annotate_from_scrape_metadata_file supports data_io and GCS
        annotate_from_scrape_metadata_file(cf = cf, scrape_metadata_filename = filename, verbose = False)
        print(f"Annotation finished for {filename}")




    #setup_logger()
    print(f"Starting watcher. location={TRACE_STORAGE_LOCATION} | prefix={PATTERN_PREFIX} | suffix={PATTERN_SUFFIX} | startup_backlog={PROCESS_STARTUP_BACKLOG}")


    # 1) Load already processed files from persistent state
    processed = load_processed_files()
    print(f"Loaded {len(processed)} processed files")
    existing_files = find_candidate_files()
    print(f"Found {len(existing_files)} existing file(s) in watch location")

    if PROCESS_STARTUP_BACKLOG:
        # Process files that are not yet marked as processed
        backlog = sorted(f for f in existing_files if f not in processed)
        print(f"Startup backlog mode ON: {len(backlog)} existing file(s) to process")

        for filename in backlog:
            try:
                handle_new_file(filename)
                mark_processed(filename, processed)
            except Exception as e:
                print(f"Error processing backlog file {filename}: {e}")
    else:
        # Treat all existing files as already processed for this run
        print(f"Startup backlog mode OFF: skipping {len(existing_files)} existing file(s)")
        for filename in existing_files:
            if filename not in processed:
                # Only update the in-memory set; do not touch the log
                processed.add(filename)

    # 3) Main loop: watch for new files forever
    try:
        while True:
            candidates = find_candidate_files()
            new_files = sorted(f for f in candidates if f not in processed)

            if new_files:
                print(f"\nFound {len(new_files)} new unprocessed file(s)")

            for ii, filename in enumerate(new_files):
                try:
                    handle_new_file(filename)
                    mark_processed(filename, processed)
                except Exception as e:
                    print(f"Error while processing {filename}: {e}")
                
                if ii == len(new_files) - 1:
                    print("Done - Back to waiting for a new file to appear in the folder...\n")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("KeyboardInterrupt received. Shutting down.")




if __name__ == "__main__":
    main()
