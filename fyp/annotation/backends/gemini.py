"""Gemini annotation backend — a thin adapter over the existing pipeline.

The Gemini call path itself stays in :mod:`fyp.annotation.machine_annotation`
(``initialize_machine`` / ``call_machine`` / ``_generate_with_retry``); this
class only gives it the common :class:`AnnotationBackend` surface so backend
selection, availability gating and version bookkeeping are uniform across
backends. Deliberately no behavior change: ``availability()`` reproduces the
``annotation_configured`` reasons byte-identically, and
``version_extra_params()`` returns ``{}`` so existing ``av_`` hashes are
untouched.
"""

import fyp.core.gemini_client as gemini_client
from fyp.annotation.backends.base import AnnotationBackend, BackendAvailability
from fyp.fyp_config import get_config






class GeminiBackend(AnnotationBackend):
    """The production Gemini (Vertex AI or API-key) backend."""

    name = "gemini"
    max_workers = 50
    supports_batch_mode = True
    cloud_run_capable = True


    def availability(self, deep: bool = False) -> BackendAvailability:
        """Config (and optionally live) readiness of Gemini annotation.

        The shallow check is pure config — the same rules as the historical
        ``annotation_configured()``: credentials resolve via ``gemini_mode``,
        and GCS-stored media requires Vertex (a ``gs://`` URI is unreadable to
        the plain API-key client).

        Args:
            deep: When True, additionally issue a ~1-token generation call to
                prove auth/quota/model.

        Returns:
            The availability result.
        """
        checks: list[dict] = []

        mode, reason = gemini_client.gemini_mode()
        checks.append({"name": "credentials", "ok": mode is not None,
                       "detail": reason if mode is None else f"mode: {mode}",
                       "fix": "" if mode is not None else
                       "See docs/installation.md#enabling-gemini-later."})
        if mode is None:
            return BackendAvailability(ok=False, reason=reason, checks=checks)

        if mode == gemini_client.MODE_API_KEY and get_config()["data_io"]["use_gcs_for_media"]:
            reason = (
                "Gemini annotation is not configured: media is stored on GCS "
                "(use_gcs_for_media = true), which is passed to Gemini as a gs:// "
                "URI that only Vertex AI can read. The plain Gemini API can only "
                "annotate media on local disk. Either configure Vertex AI "
                "([machine].vertexai = true with a GCP project), or set "
                "use_gcs_for_media = false to store media locally. "
                "See docs/installation.md#enabling-gemini-later."
            )
            checks.append({"name": "media access", "ok": False, "detail": reason,
                           "fix": "Configure Vertex AI or store media locally."})
            return BackendAvailability(ok=False, reason=reason, checks=checks)

        checks.append({"name": "media access", "ok": True, "detail": "", "fix": ""})

        if deep:
            ping = self._ping()
            checks.append(ping)
            if not ping["ok"]:
                return BackendAvailability(ok=False, reason=ping["detail"], checks=checks)

        return BackendAvailability(ok=True, reason="", checks=checks)


    def _ping(self) -> dict:
        """Issue a ~1-token generation call; return a check row."""
        import google.genai

        import fyp.machine_annotation as machine_annotation

        machine_annotation.initialize_machine()
        client = get_config()["machine"]["gemini"].get("client")
        if client is None:
            return {"name": "api ping", "ok": False,
                    "detail": "Gemini client failed to initialize (offline or bad credentials)",
                    "fix": "Check credentials / network."}
        model = get_config()["machine"]["gemini"]["model"]
        try:
            client.models.generate_content(
                model=model, contents="ping",
                config=google.genai.types.GenerateContentConfig(
                    max_output_tokens=1,
                    thinking_config=google.genai.types.ThinkingConfig(thinking_budget=0)))
            return {"name": "api ping", "ok": True, "detail": f"{model} responded", "fix": ""}
        except Exception as e:
            return {"name": "api ping", "ok": False,
                    "detail": f"{model} generation call failed: {e!r}",
                    "fix": "Check model name, credentials and quota."}


    def annotate_one(self, item_id: str, platform: str | None = None,
                     gen_overrides: dict | None = None,
                     prompt_text: str | None = None,
                     response_schema=None) -> dict:
        """Annotate one item via the existing ``call_machine`` path.

        Args:
            item_id: The item to annotate.
            platform: The item's source platform.
            gen_overrides: Optional per-call generation overrides (A/B arms);
                merged over the instance's variant overrides (arm wins). Empty
                on the default variant means the exact historical path.
            prompt_text: Ignored (production always uses the active prompt).
            response_schema: Ignored (production schema).

        Returns:
            The production raw-row dict.
        """
        import fyp.machine_annotation as machine_annotation

        merged = {**self.overrides, **(gen_overrides or {})}
        return machine_annotation.call_machine(item_id, platform=platform,
                                               gen_overrides=merged or None)
