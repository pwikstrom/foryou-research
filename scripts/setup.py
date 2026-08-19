"""Interactive first-run setup wizard for a local For You Data Hub installation.

Run from the repository root, before (or after) installing Python
dependencies — the wizard itself is stdlib-only:

    python scripts/setup.py                 # interactive
    python scripts/setup.py --check-only    # environment checks only
    python scripts/setup.py --install       # also create .venv + pip install
    python scripts/setup.py --verify        # post-install: live-check config
    python scripts/setup.py --data-dir ~/fyp_local --no-gemini --yes

It writes ``config/config.local.toml`` (a gitignored overlay deep-merged
over the committed ``config/config.toml``) and, optionally, appends missing
keys to a ``.env`` file. It never edits the committed config.

Platform selection is informational only — it tailors the printed guidance
(cookies, ffmpeg, node/deno). There is deliberately no enable/disable config
key for platforms: the platform list from ``config/scrape_contract.toml``
feeds media-path resolution of already-stored data, worker-process naming,
and queue validation, so filtering it would ripple into read paths of
existing data, while an unused platform already costs nothing (its queue
just stays empty).
"""

import argparse
import contextlib
import io
import os
import secrets
import shutil
import subprocess
import sys
import tempfile

if sys.version_info < (3, 11):  # noqa: UP036 - the whole point is catching old interpreters
    # tomllib (imported just below) exists only from Python 3.11, so on an
    # older interpreter the import line would crash with a bare
    # ModuleNotFoundError before the wizard's own Python check can run.
    # Catch it here, in plain English, with the exact command to run instead.
    _ver = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if shutil.which("python3.12"):
        _fix = "Python 3.12 is installed on this machine - run:\n\n    python3.12 scripts/setup.py"
    else:
        _fix = (
            "Install Python 3.12 first:\n"
            "  macOS:   brew install python@3.12\n"
            "  Linux:   sudo apt install python3.12 python3.12-venv\n"
            "  Windows: https://www.python.org/downloads/\n"
            "then run:  python3.12 scripts/setup.py"
        )
    sys.exit(f"error: this wizard needs Python 3.12, but you ran it with Python {_ver}.\n{_fix}")

import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_LOCAL = REPO_ROOT / "config" / "config.local.toml"
ENV_FILE = REPO_ROOT / ".env"
ALL_PLATFORMS = ("tiktok", "instagram", "youtube")
GEMINI_MODES = ("off", "api_key", "vertex")






@dataclass
class Answers:
    """Collected wizard answers that drive the generated files."""

    data_dir: str = "~/fyp_local"
    media_dir: str = ""
    platforms: list[str] = field(default_factory=lambda: list(ALL_PLATFORMS))
    gemini_mode: str = "off"
    vertex_project: str = ""
    gemini_api_key: str = ""
    gcs: bool = False
    gcs_bucket: str = ""
    contact_email: str = ""
    flask_secret: str = ""






@dataclass
class CheckResult:
    """One environment-check outcome.

    ``level`` controls grouping and exit-code semantics: "required" failures
    make ``--check-only`` exit non-zero; "recommended" and "optional" are
    advisory; "info" rows are never counted as failures at all (they describe
    something that needs no action right now, e.g. a tool pip installs later).
    """

    name: str
    ok: bool
    detail: str
    needed_for: str
    level: str = "required"






def resolve_media_dir(answers: Answers) -> str:
    """Return the effective media directory (default: ``<data_dir>/media``)."""
    return answers.media_dir or os.path.join(answers.data_dir, "media")






def free_space_gb(path: str) -> float | None:
    """Free disk space (GB) on the volume holding ``path``.

    The path itself may not exist yet — the nearest existing ancestor is
    measured instead. Returns None when nothing can be measured.
    """
    probe = Path(os.path.abspath(os.path.expanduser(path)))
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free / 1e9
    except OSError:
        return None






def build_config_toml(answers: Answers, generated_on: str = "") -> str:
    """Render the ``config.local.toml`` overlay text for the given answers.

    Only overridden keys are emitted; everything else stays inherited from
    the committed ``config/config.toml``. The result is validated by
    round-tripping through ``tomllib`` before being written.

    Args:
        answers: The collected wizard answers.
        generated_on: Optional date string for the header comment.

    Returns:
        The full TOML text for ``config/config.local.toml``.

    Raises:
        ValueError: If the rendered text is not valid TOML (template bug).
    """
    stamp = f" on {generated_on}" if generated_on else ""
    lines = [
        f"# Generated by scripts/setup.py{stamp} - re-run the wizard or edit freely.",
        "# This gitignored overlay is deep-merged over config/config.toml.",
        "",
        "[paths]",
        f'local_data = "{answers.data_dir}"',
        f'local_media = "{resolve_media_dir(answers)}"',
    ]

    lines += ["", "[data_io]"]
    flag = "true" if answers.gcs else "false"
    lines += [
        f"use_gcs_for_data = {flag}",
        f"use_gcs_for_media = {flag}",
        f"use_gcs_for_cache = {flag}",
    ]
    if answers.gcs and answers.gcs_bucket:
        lines.append(f'GCS_bucket_name = "{answers.gcs_bucket}"')

    if answers.gemini_mode == "vertex":
        lines += ["", "[machine.gemini]", f'project = "{answers.vertex_project}"']
    elif answers.gemini_mode == "api_key":
        lines += ["", "[machine.gemini]", "vertexai = false"]
    else:
        # Gemini off: leave the committed defaults alone, but say so here —
        # this file is where someone looks when they change their mind, and
        # "no [machine.gemini] section" is otherwise a silent, unexplained state.
        lines += [
            "",
            "# Gemini annotation is off (no [machine.gemini] overrides).",
            "# To turn it on later, uncomment ONE of the two blocks below and",
            "# restart the app - or just re-run: python scripts/setup.py",
            "#",
            "# Plain Gemini API (easiest; no Google Cloud account needed).",
            "# Both the line below AND the GEMINI_API_KEY environment variable",
            "# are required - the key alone is not enough, because vertexai",
            "# defaults to true and decides which service is used.",
            "# [machine.gemini]",
            "# vertexai = false",
            "#",
            "# Vertex AI (needs a GCP project with billing + the Vertex AI API,",
            "# authenticated via: gcloud auth application-default login).",
            "# [machine.gemini]",
            "# vertexai = true",
            '# project = "your-gcp-project-id"',
            "#",
            "# Full walkthrough: docs/installation.md#enabling-gemini-later",
        ]

    if answers.contact_email:
        lines += ["", "[site]", f'contact_email = "{answers.contact_email}"']

    text = "\n".join(lines) + "\n"
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Generated config is not valid TOML: {exc}") from exc
    return text






def build_env_file(answers: Answers, existing_text: str) -> str:
    """Merge collected env values into ``.env`` text, append-only.

    Existing lines are preserved verbatim; a key already present (even
    commented out with a value) is never rewritten — the skipped keys are
    reported by the caller.

    Args:
        answers: The collected wizard answers.
        existing_text: Current ``.env`` content ("" when the file is new).

    Returns:
        The merged ``.env`` text.
    """
    additions: dict[str, str] = {}
    if answers.gemini_api_key:
        additions["GEMINI_API_KEY"] = answers.gemini_api_key
    if answers.flask_secret:
        additions["FLASK_SECRET_KEY"] = answers.flask_secret
    if answers.gcs and answers.gcs_bucket:
        additions["FYP_GCS_BUCKET_NAME"] = answers.gcs_bucket

    existing_keys = set()
    for line in existing_text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if "=" in stripped:
            existing_keys.add(stripped.split("=", 1)[0].strip())

    new_lines = [f"{k}={v}" for k, v in additions.items() if k not in existing_keys]
    if not new_lines:
        return existing_text

    text = existing_text
    if text and not text.endswith("\n"):
        text += "\n"
    if not text:
        text = "# Written by scripts/setup.py. Loaded automatically when the app starts;\n" \
               "# values already exported in the shell always take precedence.\n"
    return text + "\n".join(new_lines) + "\n"






def validate_data_dir(raw: str) -> tuple[str, str]:
    """Validate a proposed data directory.

    Args:
        raw: The user-supplied path (may contain ``~`` or be relative).

    Returns:
        A ``(resolved_path, problem)`` tuple. ``problem`` is "" when the
        path is usable, otherwise a human-readable reason.
    """
    resolved = os.path.abspath(os.path.expanduser(raw.strip()))

    if os.path.isfile(resolved):
        return resolved, "path exists and is a file, not a directory"

    probe_dir = Path(resolved)
    while not probe_dir.exists() and probe_dir.parent != probe_dir:
        probe_dir = probe_dir.parent
    if not os.access(probe_dir, os.W_OK):
        return resolved, f"not writable (nearest existing ancestor: {probe_dir})"
    if os.path.isdir(resolved):
        try:
            with tempfile.NamedTemporaryFile(dir=resolved):
                pass
        except OSError as exc:
            return resolved, f"directory is not writable: {exc}"

    try:
        inside_repo = Path(resolved).is_relative_to(REPO_ROOT)
    except ValueError:
        inside_repo = False
    if inside_repo:
        return resolved, "inside the repository checkout (data would mix with code)"
    return resolved, ""






def check_environment(include_local_models: bool = False) -> list[CheckResult]:
    """Probe the local environment for required and optional tooling.

    Args:
        include_local_models: Also run the (chatty, rarely relevant on a first
            install) readiness checks for the optional local Qwen/MiniCPM
            annotation backends. Off by default; ``--verbose`` turns it on.
    """
    results = [
        CheckResult(
            name="python",
            ok=sys.version_info >= (3, 12),
            detail=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            needed_for="required (the project targets Python 3.12)",
            level="required",
        ),
        CheckResult(
            name="virtualenv",
            ok=sys.prefix != sys.base_prefix,
            detail=sys.prefix if sys.prefix != sys.base_prefix else "not active",
            needed_for="recommended (create with: python3.12 -m venv .venv)",
            level="recommended",
        ),
    ]
    tools = (
        ("ffmpeg", "YouTube HD (DASH) media merges only - slideshows use the bundled imageio-ffmpeg"),
        ("node", "YouTube media downloads from datacenter IPs (not needed on home networks)"),
        ("deno", "alternative JS runtime for the same YouTube path as node"),
    )
    for tool, needed_for in tools:
        path = shutil.which(tool)
        results.append(CheckResult(
            name=tool, ok=bool(path), detail=path or "not found",
            needed_for=needed_for, level="optional",
        ))
    # yt-dlp arrives with `pip install -r requirements-dev.txt` — missing
    # before that step is the normal state, not a problem to go fix.
    ytdlp = shutil.which("yt-dlp")
    results.append(CheckResult(
        name="yt-dlp",
        ok=bool(ytdlp),
        detail=ytdlp or "not found - installed automatically by pip in the install step; nothing to do now",
        needed_for="scraping (comes with the project requirements)",
        level="info",
    ))
    if sys.platform not in ("darwin", "win32"):
        results.append(CheckResult(
            name="browser cookies",
            ok=False,
            detail="Linux: Chrome-profile cookie extraction is macOS-only",
            needed_for="authenticated scraping (Instagram needs cookies; TikTok/YouTube degrade to public access)",
            level="info",
        ))
    if include_local_models:
        results.extend(check_local_qwen())
        results.extend(check_local_minicpm())
    return results






def check_local_qwen() -> list[CheckResult]:
    """Readiness checks for the OPTIONAL local Qwen annotation backend.

    Delegates to :mod:`fyp.annotation.backends.qwen_support` (the same checks
    the admin UI's requirements panel shows). On a non-Apple-Silicon host the
    first check simply reports that the backend is unsupported there — the
    Gemini backend is unaffected either way.
    """
    try:
        # setup.py runs pre-install from a plain checkout — make the repo root
        # importable so the shared check module can be reused. Importing fyp
        # prints config-boot noise, so swallow stdout/stderr around it.
        root = str(Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from fyp.annotation.backends import qwen_support

            checks = qwen_support.check_all()
    except Exception as exc:
        return [CheckResult(
            name="local qwen annotation",
            ok=False,
            detail=f"checks unavailable: {exc}",
            needed_for="optional local Qwen annotation backend",
            level="optional",
        )]
    results = []
    for check in checks:
        needed = "optional local Qwen annotation backend"
        if not check["ok"] and check["fix"]:
            needed += f" — fix: {check['fix']}"
        results.append(CheckResult(
            name=f"local qwen: {check['name']}",
            ok=check["ok"],
            detail=check["detail"],
            needed_for=needed,
            level="optional",
        ))
    return results






def check_local_minicpm() -> list[CheckResult]:
    """Readiness checks for the OPTIONAL local MiniCPM annotation backend.

    Delegates to :mod:`fyp.annotation.backends.minicpm_support` (the same
    checks the admin UI's requirements panel shows). On a non-Apple-Silicon
    host the first check simply reports that the backend is unsupported there.
    """
    try:
        root = str(Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            from fyp.annotation.backends import minicpm_support

            checks = minicpm_support.check_all()
    except Exception as exc:
        return [CheckResult(
            name="local minicpm annotation",
            ok=False,
            detail=f"checks unavailable: {exc}",
            needed_for="optional local MiniCPM annotation backend",
            level="optional",
        )]
    results = []
    for check in checks:
        needed = "optional local MiniCPM annotation backend"
        if not check["ok"] and check["fix"]:
            needed += f" — fix: {check['fix']}"
        results.append(CheckResult(
            name=f"local minicpm: {check['name']}",
            ok=check["ok"],
            detail=check["detail"],
            needed_for=needed,
            level="optional",
        ))
    return results






def prompt(question: str, default: str = "") -> str:
    """Ask one question on stdin, returning the default on empty input."""
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default






def prompt_yes_no(question: str, default: bool) -> bool:
    """Ask a yes/no question, returning the default on empty input."""
    hint = "Y/n" if default else "y/N"
    answer = input(f"{question} [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")






def load_existing_defaults() -> Answers:
    """Seed prompt defaults from an existing ``config.local.toml`` if present."""
    answers = Answers()
    if not CONFIG_LOCAL.exists():
        return answers
    try:
        existing = tomllib.loads(CONFIG_LOCAL.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return answers
    paths = existing.get("paths", {})
    answers.data_dir = paths.get("local_data", answers.data_dir)
    answers.media_dir = paths.get("local_media", "")
    data_io_cfg = existing.get("data_io", {})
    answers.gcs = bool(data_io_cfg.get("use_gcs_for_data", False))
    answers.gcs_bucket = data_io_cfg.get("GCS_bucket_name", "")
    # Gemini settings live in [machine.gemini]; a pre-restructure overlay may
    # still carry them flat in [machine] — read nested-first, then flat.
    machine = existing.get("machine", {})
    gemini = {**machine, **(machine.get("gemini", {}) or {})}
    if gemini.get("project"):
        answers.gemini_mode = "vertex"
        answers.vertex_project = gemini["project"]
    elif gemini.get("vertexai") is False:
        answers.gemini_mode = "api_key"
    answers.contact_email = existing.get("site", {}).get("contact_email", "")
    return answers






def run_interactive(defaults: Answers) -> Answers:
    """Run the interactive question flow and return the collected answers."""
    answers = Answers()
    print("\nFYP local setup - answers are written to config/config.local.toml.\n")

    while True:
        raw = prompt("Data directory (all project data except media)", defaults.data_dir)
        resolved, problem = validate_data_dir(raw)
        if not problem:
            answers.data_dir = resolved
            break
        if problem.startswith("inside the repository"):
            if prompt_yes_no(f"  Warning: {problem}. Use it anyway?", default=False):
                answers.data_dir = resolved
                break
        else:
            print(f"  Not usable: {problem}")

    default_media = defaults.media_dir or os.path.join(answers.data_dir, "media")
    answers.media_dir = os.path.abspath(os.path.expanduser(
        prompt("Media directory (video files; needs disk space)", default_media)
    ))
    free = free_space_gb(answers.media_dir)
    if free is not None:
        print(f"  Free space on that volume: {free:.0f} GB")

    raw_platforms = prompt(
        "Platforms you plan to scrape (comma-separated: tiktok,instagram,youtube)",
        ",".join(defaults.platforms),
    )
    answers.platforms = [p.strip() for p in raw_platforms.split(",") if p.strip() in ALL_PLATFORMS]

    print("\nGemini annotation (optional - everything else works without it):")
    print("  1) off        - no LLM annotation for now")
    print("  2) api key    - plain Gemini API via GEMINI_API_KEY")
    print("  3) vertex     - Vertex AI with your own GCP project (ADC auth)")
    mode_map = {"1": "off", "2": "api_key", "3": "vertex"}
    mode_default = {"off": "1", "api_key": "2", "vertex": "3"}[defaults.gemini_mode]
    answers.gemini_mode = mode_map.get(prompt("Choose", mode_default), defaults.gemini_mode)
    if answers.gemini_mode == "vertex":
        answers.vertex_project = prompt("GCP project id for Vertex AI", defaults.vertex_project)
    elif answers.gemini_mode == "api_key" and prompt_yes_no(
        "Store your GEMINI_API_KEY in .env now? (loaded automatically at startup; "
        "default: no - export it yourself)",
        default=False,
    ):
        answers.gemini_api_key = prompt("GEMINI_API_KEY value")

    answers.gcs = prompt_yes_no("Use Google Cloud Storage for data/media? (local disk is the default)", defaults.gcs)
    if answers.gcs:
        answers.gcs_bucket = prompt("GCS bucket name", defaults.gcs_bucket)

    answers.contact_email = prompt(
        "Contact email to show on the public pages (optional, Enter to skip)",
        defaults.contact_email,
    )

    if prompt_yes_no("Generate a FLASK_SECRET_KEY into .env? (optional locally, required if deployed)", default=False):
        answers.flask_secret = secrets.token_urlsafe(32)

    return answers






def answers_from_args(args: argparse.Namespace, defaults: Answers) -> Answers:
    """Build ``Answers`` non-interactively from CLI flags + defaults."""
    answers = Answers()
    resolved, problem = validate_data_dir(args.data_dir or defaults.data_dir)
    if problem and not problem.startswith("inside the repository"):
        sys.exit(f"error: data dir {resolved}: {problem}")
    answers.data_dir = resolved
    answers.media_dir = os.path.abspath(os.path.expanduser(
        args.media_dir or defaults.media_dir or os.path.join(resolved, "media")
    ))
    if args.platforms:
        answers.platforms = [p.strip() for p in args.platforms.split(",") if p.strip() in ALL_PLATFORMS]
    else:
        answers.platforms = defaults.platforms
    if args.no_gemini:
        answers.gemini_mode = "off"
    elif args.vertex_project:
        answers.gemini_mode = "vertex"
        answers.vertex_project = args.vertex_project
    elif args.gemini_api_key_mode:
        answers.gemini_mode = "api_key"
    else:
        answers.gemini_mode = defaults.gemini_mode
        answers.vertex_project = defaults.vertex_project
    if args.gcs_bucket:
        answers.gcs = True
        answers.gcs_bucket = args.gcs_bucket
    else:
        answers.gcs = defaults.gcs
        answers.gcs_bucket = defaults.gcs_bucket
    answers.contact_email = args.contact_email or defaults.contact_email
    return answers






def print_checks(results: list[CheckResult], local_models_hidden: bool = False) -> bool:
    """Print the environment-check summary, grouped by importance.

    Returns:
        True when every *required* check passed (advisory levels never flip
        this) — the ``--check-only`` exit code.
    """
    groups = (
        ("required", "Required"),
        ("recommended", "Recommended"),
        ("optional", "Optional (feature-specific)"),
        ("info", "For information (nothing to do now)"),
    )
    print("\nEnvironment checks:")
    required_ok = True
    any_tool_missing = False
    for level, title in groups:
        rows = [r for r in results if r.level == level]
        if not rows:
            continue
        print(f"\n  {title}:")
        for r in rows:
            mark = "OK " if r.ok else "-- "
            print(f"    {mark} {r.name:<16} {r.detail}")
            if not r.ok:
                if level == "required":
                    required_ok = False
                if level != "info":
                    any_tool_missing = True
                    print(f"         needed for: {r.needed_for}")
    if local_models_hidden:
        print(
            "\n  (Checks for the optional local Qwen/MiniCPM annotation backends are\n"
            "   hidden - re-run with --verbose to see them.)"
        )
    if any_tool_missing:
        print(
            "\n  Note: 'not found' means the tool is not on your PATH. If you just\n"
            "  installed it, open a new terminal; otherwise see the 'PATH and\n"
            "  environment variables' section of docs/installation.md."
        )
    return required_ok






def print_next_steps(answers: Answers, in_venv: bool, installed: bool = False) -> None:
    """Print the final what-to-do-next summary (platform-aware)."""
    windows = os.name == "nt"
    activate = r".venv\Scripts\activate" if windows else "source .venv/bin/activate"
    venv_create = "py -3.12 -m venv .venv" if windows else "python3.12 -m venv .venv"

    print("\nDone. Wrote config/config.local.toml.")
    print("\nNext steps:")
    step = 1
    if not installed:
        if not in_venv:
            print(f"  {step}. {venv_create}")
            step += 1
            print(f"  {step}. {activate}")
            step += 1
        print(f"  {step}. pip install -r requirements-dev.txt && pip install -e .")
        step += 1
    elif not in_venv:
        print(f"  {step}. {activate}")
        step += 1
    print(f"  {step}. python web_interface/fyp_data_hub.py   ->  http://localhost:5002")
    step += 1
    print(f"  {step}. FIRST BOOT prints a one-time random password for admin@admin.net -")
    print("     copy it from the console and change it after logging in.")
    step += 1
    print(f"  {step}. python scripts/setup.py --verify   (live-checks your configuration)")
    step += 1
    if windows:
        print(f'  {step}. python -m pytest -q -m "not requires_data and not requires_gcs and not slow"')
    else:
        print(f"  {step}. bash scripts/verify.sh   (checks the install: lint + tests + import smoke)")

    if answers.gemini_api_key or answers.flask_secret:
        print("\n  Values in .env are loaded automatically when the app starts;")
        print("  anything you have exported in the shell takes precedence.")

    if answers.gemini_mode == "off":
        print("\nGemini annotation is off. Everything else works without it;")
        print("machine annotation, embeddings, the semantic map and the")
        print("Correlations tab stay unavailable until it is configured.")
        print("To turn it on later, re-run this wizard or see:")
        print("  docs/installation.md#enabling-gemini-later")
    elif answers.gemini_mode == "api_key" and not answers.gemini_api_key:
        print("\nGemini is set to the plain API - remember to provide the key:")
        print("  export GEMINI_API_KEY=your-key-here")
        print("or put GEMINI_API_KEY=... in a .env file at the project root")
        print("(loaded automatically at startup).")

    notes = []
    if "youtube" in answers.platforms:
        notes.append("youtube: install ffmpeg for HD merges; node/deno only matter on datacenter IPs")
    if "instagram" in answers.platforms:
        notes.append("instagram: scraping needs a logged-in Chrome session (cookies; macOS only locally)")
    if "tiktok" in answers.platforms:
        notes.append("tiktok: works unauthenticated for public content; Chrome cookies improve access")
    if notes:
        print("\nPlatform notes:")
        for n in notes:
            print(f"  - {n}")
    print("\nFull guide: docs/installation.md")






def _python312_command() -> list[str] | None:
    """The command prefix for a Python 3.12 interpreter, or None if absent."""
    if sys.version_info[:2] == (3, 12):
        return [sys.executable]
    exe = shutil.which("python3.12")
    if exe:
        return [exe]
    if os.name == "nt" and shutil.which("py"):
        return ["py", "-3.12"]
    return None






def run_install() -> bool:
    """Create ``.venv`` (if needed) and install the project's dependencies.

    Runs inside an already-active virtualenv when there is one; otherwise
    creates ``.venv`` with Python 3.12 and installs into it. Output streams
    to the console. Returns True on success.
    """
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        venv_python = Path(sys.executable)
        steps = []
    else:
        py312 = _python312_command()
        if py312 is None:
            print(
                "\nCannot install: no Python 3.12 interpreter found on PATH.\n"
                "Install it first (macOS: brew install python@3.12; Linux:\n"
                "apt install python3.12 python3.12-venv; Windows: python.org)\n"
                "and re-run: python3.12 scripts/setup.py --install"
            )
            return False
        venv_dir = REPO_ROOT / ".venv"
        if os.name == "nt":
            venv_python = venv_dir / "Scripts" / "python.exe"
        else:
            venv_python = venv_dir / "bin" / "python"
        steps = []
        if not venv_python.exists():
            steps.append(([*py312, "-m", "venv", str(venv_dir)],
                          "Creating virtual environment .venv"))

    steps += [
        ([str(venv_python), "-m", "pip", "install", "-r",
          str(REPO_ROOT / "requirements-dev.txt")],
         "Installing dependencies (this takes a few minutes)"),
        ([str(venv_python), "-m", "pip", "install", "-e", str(REPO_ROOT)],
         "Installing the fyp package (editable)"),
    ]
    for cmd, label in steps:
        print(f"\n== {label}")
        print(f"   $ {' '.join(cmd)}")
        result = subprocess.run(cmd, cwd=REPO_ROOT)
        if result.returncode != 0:
            print(
                "\nThat step failed - see the output above for the reason.\n"
                "Once fixed, re-run just the install with:\n"
                "    python scripts/setup.py --install"
            )
            return False
    print("\nDependencies installed.")
    return True






def run_verify() -> int:
    """Post-install live verification of the configured services.

    Confirms the installed app imports, reports what ``.env`` supplies, and
    actually exercises the configured credentials: a free Gemini
    ``count_tokens`` call and a GCS bucket-existence probe. Unconfigured
    optional services are reported, not failed. Returns the exit code.
    """
    print("\nVerifying the installed app against your configuration...")
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            root = str(REPO_ROOT)
            if root not in sys.path:
                sys.path.insert(0, root)
            from fyp.core import fyp_config

            cf = fyp_config.get_config()
    except Exception as exc:
        print(f"  --  app import        failed: {exc}")
        print("       Install the dependencies first (python scripts/setup.py --install),")
        print("       then run this from the virtualenv:")
        activate = r".venv\Scripts\activate" if os.name == "nt" else "source .venv/bin/activate"
        print(f"       {activate} && python scripts/setup.py --verify")
        return 1
    print("  OK  app import        fyp package + config load")

    if ENV_FILE.exists():
        keys = []
        for line in ENV_FILE.read_text().splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                keys.append(stripped.removeprefix("export ").split("=", 1)[0].strip())
        detail = ", ".join(keys) if keys else "present but defines no values"
        print(f"  OK  .env              auto-loaded at startup: {detail}")
    else:
        print("  OK  .env              none (nothing to load; exported variables still apply)")

    problems = 0

    try:
        from fyp.annotation.machine_annotation import annotation_configured

        ok, reason = annotation_configured()
    except Exception as exc:
        ok, reason = False, f"check failed: {exc}"
    if ok:
        print("  OK  annotation        the active backend is configured")
    else:
        print(f"  --  annotation        not ready (optional): {reason}")

    try:
        from fyp.core.gemini_client import gemini_mode, make_client

        mode, _ = gemini_mode()
    except Exception:
        mode = None
    if mode:
        model = cf["machine"]["gemini"].get("model", "")
        try:
            client = make_client()
            client.models.count_tokens(model=model, contents="ping")
            print(f"  OK  gemini            live check passed ({mode} mode, model {model})")
        except Exception as exc:
            problems += 1
            print(f"  --  gemini            configured for {mode} mode, but a live call FAILED:")
            print(f"       {exc}")
            print("       Check the key/project you configured - a typo here is the usual cause.")
    else:
        print("  --  gemini            not configured (optional - annotation features stay off)")

    data_io_cfg = cf.get("data_io", {})
    if any(data_io_cfg.get(k) for k in ("use_gcs_for_data", "use_gcs_for_media", "use_gcs_for_cache")):
        bucket_name = str(data_io_cfg.get("GCS_bucket_name") or "").strip()
        if not bucket_name:
            problems += 1
            print("  --  gcs               GCS is enabled but no bucket name is set")
            print("       Set FYP_GCS_BUCKET_NAME (or GCS_bucket_name in config.local.toml).")
        else:
            try:
                from google.cloud import storage as gcs_storage

                if gcs_storage.Client().bucket(bucket_name).exists():
                    print(f"  OK  gcs               bucket '{bucket_name}' is reachable")
                else:
                    problems += 1
                    print(f"  --  gcs               bucket '{bucket_name}' not found (or no access)")
            except Exception as exc:
                problems += 1
                print(f"  --  gcs               cannot reach bucket '{bucket_name}': {exc}")
                print("       Usually fixed by: gcloud auth application-default login")
    else:
        print("  OK  storage           local disk (no GCS configured - nothing to check)")

    if problems:
        print(f"\n{problems} problem(s) found - see the lines above.")
    else:
        print("\nEverything configured checks out.")
    return 1 if problems else 0






def main() -> None:
    """Entry point: parse flags, collect answers, write outputs."""
    parser = argparse.ArgumentParser(description="For You Data Hub local setup wizard")
    parser.add_argument("--data-dir", help="data directory (default ~/fyp_local)")
    parser.add_argument("--media-dir", help="media directory (default <data-dir>/media)")
    parser.add_argument("--platforms", help="comma-separated platforms you plan to scrape")
    parser.add_argument("--no-gemini", action="store_true", help="skip Gemini configuration")
    parser.add_argument("--gemini-api-key-mode", action="store_true",
                        help="use the plain Gemini API (GEMINI_API_KEY env) instead of Vertex")
    parser.add_argument("--vertex-project", help="GCP project id for Vertex AI annotation")
    parser.add_argument("--gcs-bucket", help="use GCS with this bucket name")
    parser.add_argument("--contact-email", help="contact email shown on the public pages")
    parser.add_argument("--yes", action="store_true", help="non-interactive: accept defaults for anything unset")
    parser.add_argument("--force", action="store_true", help="overwrite config.local.toml without asking")
    parser.add_argument("--check-only", action="store_true", help="run environment checks and exit (non-zero if a required check fails)")
    parser.add_argument("--verbose", action="store_true",
                        help="include the optional local-model (Qwen/MiniCPM) backend checks")
    parser.add_argument("--install", action="store_true",
                        help="also create .venv and pip-install the dependencies")
    parser.add_argument("--verify", action="store_true",
                        help="post-install: live-check the configured services (Gemini, GCS) and exit")
    args = parser.parse_args()

    if args.verify:
        sys.exit(run_verify())

    checks = check_environment(include_local_models=args.verbose)
    required_ok = print_checks(checks, local_models_hidden=not args.verbose)
    if args.check_only:
        sys.exit(0 if required_ok else 1)

    defaults = load_existing_defaults()
    if args.yes:
        answers = answers_from_args(args, defaults)
    else:
        answers = run_interactive(defaults)

    if CONFIG_LOCAL.exists():
        if not args.force and not args.yes:
            if not prompt_yes_no(f"{CONFIG_LOCAL} exists - back it up and overwrite?", default=True):
                print("Aborted - nothing written.")
                return
        backup = CONFIG_LOCAL.with_suffix(".toml.bak")
        shutil.copy2(CONFIG_LOCAL, backup)
        print(f"Backed up existing overlay to {backup}")

    CONFIG_LOCAL.write_text(build_config_toml(answers, generated_on=date.today().isoformat()))

    if answers.gemini_api_key or answers.flask_secret or (answers.gcs and answers.gcs_bucket):
        existing = ENV_FILE.read_text() if ENV_FILE.exists() else ""
        merged = build_env_file(answers, existing)
        if merged != existing:
            ENV_FILE.write_text(merged)
            os.chmod(ENV_FILE, 0o600)
            print(f"Updated {ENV_FILE} (mode 600).")

    in_venv = sys.prefix != sys.base_prefix
    venv_python_exists = (
        REPO_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        / ("python.exe" if os.name == "nt" else "python")
    ).exists()
    # Offer to run the venv + pip steps so a first-time user never has to
    # type them; --install skips the question. Defaults to yes only when no
    # venv exists yet.
    installed = False
    if args.install or (not args.yes and prompt_yes_no(
        "\nCreate the virtualenv and install the dependencies now? (a few minutes)",
        default=not (in_venv or venv_python_exists),
    )):
        installed = run_install()

    print_next_steps(answers, in_venv, installed=installed)






if __name__ == "__main__":
    main()
