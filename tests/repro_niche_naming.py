"""Verify the niche-naming fix: the dedicated naming client must target the
generation location and return a descriptive label (not 404 -> "Niche N").

Before the fix, video_map._name_niches reused embeddings._get_client() (pinned
to the embedding location us-central1), where gemini-3-flash-preview 404s. The
fix adds video_map._get_naming_client() at the configured generation location.
"""

import fyp.fyp_config as fyp_config

fyp_config.initialize()
from fyp.fyp_config import fyp_cf
import fyp.video_map as video_map

client = video_map._get_naming_client()
model = fyp_cf["machine"]["model"]

print(f"naming model    = {model}")
print(f"naming location = {client._api_client.location}")
print("-" * 60)

prompt = (
    "These are summaries of TikTok videos in one cluster:\n"
    "- A cat knocks a glass off a table and runs away\n"
    "- A kitten attacks a roll of toilet paper\n"
    "- A cat steals food from the counter while the owner films\n\n"
    "Give a SHORT 2-4 word label naming this micro-genre. Reply with only the label."
)
resp = client.models.generate_content(model=model, contents=prompt)
print(f"naming response -> {resp.text.strip()!r}")
