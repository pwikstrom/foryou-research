# Project Instructions
## Environment
- The user is using the `.fypenv314` virtual environment.
- Always use `source .fypenv314/bin/activate` before running scripts.
## Coding Style
- Use Python type hints function signatures.
- Docstrings should follow the Google style guide.
- Make module imports at the top of the file - not inside functions.
- Use f-strings for string formatting.
- Keep functions separated by at least 5 blank lines.
- Comments should explain the code. Do not write your own reasoning in the code.
## DTypes
- Always use pyarrow dtypes for dataframes.
## Key Files
- `fyp/data_io.py`: always use this module for file access.
- `web_interface/`: contains the Flask app routes and templates.
## Data
- Save test/debug data in the `tmp/` folder.
## Test scripts
- Save test scripts in the `tests/` folder.