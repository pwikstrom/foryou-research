"""Annotation-backend base class and availability result type.

An annotation backend produces the production **raw-row dict** for one item:

    {item_id, source_platform, inference_ts, inference_duration, model,
     prompt_fn, annotation_version, structured=True,
     usage{prompt_tokens, candidates_tokens, thoughts_tokens, total_tokens},
     error, finish_reason, response}

Everything downstream of that dict (threading, flatten, refine, versioning,
queue pruning, batch ingest) is backend-agnostic, so a backend only has to get
this one shape right. Subclasses auto-register via ``__init_subclass__``,
exactly like ``BaseScraper`` in :mod:`fyp.scrape.platform_scraper`.

Backend ids are stable strings persisted in the admin settings store and in
A/B-eval run manifests.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field






@dataclass
class BackendAvailability:
    """Outcome of a backend readiness check.

    Attributes:
        ok: Whether the backend can run right now.
        reason: User-facing, actionable explanation when ``ok`` is False
            (empty string when ok).
        checks: Per-check detail rows for UI requirement panels, each
            ``{"name": str, "ok": bool, "detail": str, "fix": str}``.
    """

    ok: bool
    reason: str = ""
    checks: list = field(default_factory=list)






class AnnotationBackend(ABC):
    """One way of producing machine annotations (Gemini API, local Qwen, ...).

    Class attributes:
        name: Stable backend id (settings / manifests key).
        max_workers: Thread-pool width the orchestrator should use.
        supports_batch_mode: Whether the Gemini-Batch-API style path applies.
        cloud_run_capable: Whether the backend can run on Cloud Run (a local
            model cannot — it needs the host machine).
    """

    name: str = ""
    max_workers: int = 1
    supports_batch_mode: bool = False
    cloud_run_capable: bool = True

    _registry: dict = {}


    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.name:
            AnnotationBackend._registry[cls.name] = cls


    def __init__(self, overrides: dict | None = None, selection: str | None = None):
        """Bind the instance to a selection (a config-declared variant).

        Args:
            overrides: Config overrides layered over the implementation's own
                config block (a variant's model/param pins); empty for the
                default variant.
            selection: The settings-visible selection id; defaults to the
                implementation id (``name``).
        """
        self.overrides = dict(overrides or {})
        self.selection = selection or self.name


    @abstractmethod
    def availability(self, deep: bool = False) -> BackendAvailability:
        """Whether the backend can run, with actionable detail.

        Args:
            deep: When True, may perform a network/model probe (e.g. a 1-token
                ping); when False, must stay a cheap local config/dependency
                check safe to call on every page load.

        Returns:
            The availability result.
        """


    def prompt_suffix(self) -> str:
        """Backend-owned addendum appended to the generated contract prompt.

        A non-empty suffix changes the prompt hash and therefore (correctly)
        forks a new annotation version.

        Returns:
            The suffix text, or an empty string.
        """
        return ""


    def version_extra_params(self) -> dict:
        """Backend-specific generation params for the version identity.

        Merged into the ``av_`` version hash only when non-empty, so the
        Gemini backend must return ``{}`` to keep existing hashes stable.

        Returns:
            A dict of extra output-affecting parameters.
        """
        return {}


    def effective_model_id(self) -> str:
        """The model id this backend annotates with (version identity).

        Returns:
            The model id string (Gemini reads it from ``[machine.gemini]
            .model`` behind any variant override, so the default suits it;
            local backends override).
        """
        from fyp.fyp_config import get_config

        return self.overrides.get("model", get_config()["machine"]["gemini"]["model"])


    def version_gen_params(self) -> dict:
        """The standard five generation params as this backend runs them.

        Keys mirror ``annotation_versioning._VERSION_GEN_PARAM_KEYS``; a
        backend without a concept for a key reports ``None``. Variant
        overrides win over the ``[machine.gemini]`` values.

        Returns:
            ``{use_structured_output, temperature, thinking_budget,
            media_resolution, max_output_tokens}``.
        """
        from fyp.fyp_config import get_config

        machine = {**get_config()["machine"]["gemini"], **self.overrides}
        return {key: machine.get(key) for key in
                ("use_structured_output", "temperature", "thinking_budget",
                 "media_resolution", "max_output_tokens")}


    @abstractmethod
    def annotate_one(self, item_id: str, platform: str | None = None,
                     gen_overrides: dict | None = None,
                     prompt_text: str | None = None,
                     response_schema=None) -> dict:
        """Annotate one item and return the production raw-row dict.

        Args:
            item_id: The item to annotate.
            platform: The item's source platform (drives media resolution).
            gen_overrides: Optional per-call generation overrides
                (model / temperature / ...), used by the A/B eval harness.
            prompt_text: Optional explicit system prompt (an A/B arm's
                candidate contract); None = the active versioned prompt.
            response_schema: Optional explicit response schema matching
                ``prompt_text``; None = the active contract's schema.

        Returns:
            The raw-row dict described in the module docstring. Failures are
            reported in-band (``error`` set, ``finish_reason`` starting with
            ``"DNF"``), never raised, so batch orchestration stays uniform.
        """
