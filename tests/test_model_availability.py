import os
import sys

import google.genai
import google.genai.types


PROJECT = os.environ.get("GCP_PROJECT_ID", "")
LOCATION = "global"
MODELS = ["gemini-3-flash-preview", "gemini-2.5-flash"]


def main() -> None:
    """Probe each candidate model through the app's Vertex AI client config."""
    client = google.genai.Client(
        vertexai=True,
        project=PROJECT,
        location=LOCATION,
        http_options=google.genai.types.HttpOptions(api_version="v1"),
    )

    for model in MODELS:
        try:
            resp = client.models.generate_content(
                model=model,
                contents="Reply with the single word: ok",
            )
            print(f"AVAILABLE  {model} -> {resp.text!r}")
        except Exception as e:
            print(f"UNAVAILABLE {model} -> {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
