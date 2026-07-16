#!/usr/bin/env python3
"""Shared Google GenAI client construction for every Gemini consumer.

Gemini is reachable two ways, chosen by config:

* **Vertex AI** — ``[machine].vertexai = true`` plus a ``[machine].project``,
  authenticated by Application Default Credentials (``gcloud auth
  application-default login``).
* **The plain Gemini API** — ``[machine].vertexai = false`` plus a
  ``GEMINI_API_KEY`` (loaded into ``[machine].key`` at config init).

Annotation, embeddings, and niche naming must resolve that choice identically.
When they disagree, a config that passes the pre-flight gate still fails at
call time — so :func:`gemini_mode` is the single source of truth and
:func:`make_client` is the only supported way to build a client.

``google.genai.Client`` rejects ``project``/``location`` and ``api_key``
together ("Project/location and API key are mutually exclusive in the client
initializer"), so the two modes' kwargs must never be mixed. Note also that a
Vertex client with an empty project *constructs* happily and only fails when a
request is made, which is why the config-level check exists at all.
"""

import google.genai

from fyp.core.logging_setup import get_logger

logger = get_logger(__name__)

MODE_VERTEX = "vertex"
MODE_API_KEY = "api_key"

_FALLBACK_WARNED = False


def _cf():
    """Lazy fyp_config config-dict accessor (breaks the import cycle)."""
    from fyp.core.fyp_config import fyp_cf

    return fyp_cf


class GeminiNotConfiguredError(RuntimeError):
    """Raised when a Gemini client is requested but no mode is usable."""


def gemini_mode() -> tuple[str | None, str]:
    """Resolve the configured way of reaching Gemini.

    A pure config check — no network, no client construction. Vertex AI needs a
    GCP project; the plain Gemini API needs a key. When Vertex is requested but
    no project is set, an available ``GEMINI_API_KEY`` is used instead: Vertex
    cannot work without a project, so falling back is strictly better than
    failing, and no working Vertex configuration can be shadowed by it.

    Returns:
        ``(mode, reason)`` where ``mode`` is :data:`MODE_VERTEX`,
        :data:`MODE_API_KEY`, or ``None`` when Gemini is unusable. ``reason``
        is a user-facing explanation of what to configure when ``mode`` is
        ``None``, and an empty string otherwise.
    """
    global _FALLBACK_WARNED

    machine = _cf()["machine"]
    project = str(machine.get("project") or "").strip()
    key = str(machine.get("key") or "").strip()

    if bool(machine.get("vertexai")):
        if project:
            return MODE_VERTEX, ""
        if key:
            if not _FALLBACK_WARNED:
                logger.warning(
                    "Vertex AI is enabled ([machine].vertexai = true) but no "
                    "[machine].project is set, so Vertex cannot be used. "
                    "Falling back to the plain Gemini API with GEMINI_API_KEY. "
                    "Set vertexai = false in config/config.local.toml to make "
                    "this permanent, or set a project to use Vertex."
                )
                _FALLBACK_WARNED = True
            return MODE_API_KEY, ""
        return None, (
            "Gemini is not configured: Vertex AI is enabled "
            "([machine].vertexai = true) but no GCP project is set, and no "
            "GEMINI_API_KEY is available. Either set [machine].project in "
            "config/config.local.toml, or set vertexai = false and provide a "
            "GEMINI_API_KEY. See docs/installation.md#enabling-gemini-later."
        )

    if key:
        return MODE_API_KEY, ""
    return None, (
        "Gemini is not configured: the plain Gemini API is selected "
        "([machine].vertexai = false) but GEMINI_API_KEY is not set. Note "
        "that .env is not auto-loaded — export the key or run "
        "'set -a; source .env; set +a'. Alternatively enable Vertex AI "
        "(vertexai = true with a GCP project). "
        "See docs/installation.md#enabling-gemini-later."
    )


def _strip_api_version(http_options):
    """Return ``http_options`` with any ``api_version`` cleared.

    ``[machine].http_options_api_version`` (``v1``) is a *Vertex* API version.
    The plain Gemini API versions its surface separately and serves system
    instructions, structured output, and thinking config on ``v1beta`` — asking
    it for ``v1`` fails the request with "Unknown name systemInstruction".
    Clearing the field lets the SDK apply the right default per backend while
    preserving everything else the caller set (notably the timeout).

    Args:
        http_options: A ``google.genai.types.HttpOptions``, or None.

    Returns:
        A copy without ``api_version``, or None when nothing was passed.
    """
    if http_options is None:
        return None
    return http_options.model_copy(update={"api_version": None})


def make_client(location: str | None = None, http_options=None) -> google.genai.Client:
    """Build a GenAI client for whichever mode is configured.

    Args:
        location: Vertex region for this client, overriding ``[machine]
            .location``. Ignored in API-key mode, where the region is not a
            concept the endpoint accepts.
        http_options: Optional ``google.genai.types.HttpOptions`` to apply. Its
            ``api_version`` is honoured for Vertex and dropped for the plain
            Gemini API — see :func:`_strip_api_version`.

    Returns:
        A configured :class:`google.genai.Client`.

    Raises:
        GeminiNotConfiguredError: When no usable mode is configured; the
            message explains what to set.
    """
    mode, reason = gemini_mode()
    if mode is None:
        raise GeminiNotConfiguredError(reason)

    machine = _cf()["machine"]

    if mode == MODE_VERTEX:
        kwargs = {}
        if http_options is not None:
            kwargs["http_options"] = http_options
        return google.genai.Client(
            vertexai=True,
            project=str(machine.get("project") or "").strip(),
            location=location or machine.get("location"),
            **kwargs,
        )

    # API-key mode: project/location must not accompany api_key. A stale
    # [machine].project left over from a Vertex setup is deliberately dropped.
    kwargs = {}
    stripped = _strip_api_version(http_options)
    if stripped is not None:
        kwargs["http_options"] = stripped
    return google.genai.Client(
        vertexai=False,
        api_key=str(machine.get("key") or "").strip(),
        **kwargs,
    )
