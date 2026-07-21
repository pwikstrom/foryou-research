"""Smoke-test hosted annotation APIs (Qwen/DashScope omni + Kimi K3) on real items.

Probes the two candidate hosted backends against a few videos from the existing
pilot eval set (~/qwen_eval_work, built by scripts/adhoc/qwen_eval/01_build_eval_set.py),
using the production contract prompt + response schema dumped there. Answers the
open questions from the backend assessment:

  * Does the endpoint accept our mp4s (base64 data URL vs file-upload)?
  * Does JSON-mode / structured output hold for the contract schema?
  * Does the model hear audio (transcript field vs the Gemini reference)?
  * What do latency and token usage (→ cost) look like per item?

Keys (env):
    DASHSCOPE_API_KEY   — Alibaba Model Studio (international/Singapore region)
    MOONSHOT_API_KEY    — Moonshot/Kimi platform

Run (from repo root; venv only needs `requests`):

    DASHSCOPE_API_KEY=... MOONSHOT_API_KEY=... \
        .venv/bin/python scripts/adhoc/api_backend_smoke.py --n 3

Options:
    --providers qwen,kimi     which providers to probe
    --qwen-model / --kimi-model   model id overrides
    --n                       items to test (shortest-duration first)
    --workdir                 pilot workdir (default ~/qwen_eval_work)

Results (raw responses + errors) are saved to <workdir>/api_smoke_results.json.
"""

import argparse
import base64
import json
import os
import re
import time
from pathlib import Path

import requests


QWEN_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
KIMI_BASE = "https://api.moonshot.ai/v1"
TIMEOUT = 600


def load_fixtures(workdir: Path, n: int) -> tuple[str, dict, list[dict], dict]:
    """Load prompt, schema, the n shortest eval items, and the Gemini reference."""
    prompt = (workdir / "prompt.txt").read_text()
    schema = json.loads((workdir / "response_schema.json").read_text())
    manifest = json.loads((workdir / "eval_manifest.json").read_text())
    manifest = [m for m in manifest if Path(m["video_file"]).exists()]
    manifest.sort(key=lambda m: m.get("duration") or 1e9)
    reference = json.loads((workdir / "gemini_reference.json").read_text())
    return prompt, schema, manifest[:n], reference






def build_prompt(contract_prompt: str, schema: dict) -> str:
    """Contract prompt + explicit schema-adherence suffix (mirrors the local backends)."""
    return (
        f"{contract_prompt}\n\n"
        "Respond with ONLY a single JSON object (no markdown fences, no prose) "
        "that conforms exactly to this JSON schema:\n"
        f"{json.dumps(schema)}"
    )






def video_data_url(path: str) -> str:
    raw = Path(path).read_bytes()
    return "data:video/mp4;base64," + base64.b64encode(raw).decode()






def parse_json_response(text: str) -> dict | None:
    """Parse the model output as JSON, tolerating markdown fences."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None






def post_chat(base: str, key: str, payload: dict, stream: bool) -> tuple[dict | None, str, dict]:
    """POST a chat completion; return (message-ish result, error, usage).

    For stream=True aggregates SSE deltas (DashScope omni models require
    streaming). Result is {"content": str}; usage comes from the final chunk /
    the response body.
    """
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"{base}/chat/completions"
    if not stream:
        r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:2000]}", {}
        body = r.json()
        msg = body["choices"][0]["message"]
        return {"content": msg.get("content") or ""}, "", body.get("usage") or {}
    payload = dict(payload, stream=True, stream_options={"include_usage": True})
    r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT, stream=True)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:2000]}", {}
    content, usage = [], {}
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            if choice.get("finish_reason"):
                usage["finish_reason"] = choice["finish_reason"]
    return {"content": "".join(content)}, "", usage






def call_qwen(key: str, model: str, prompt: str, video_path: str) -> tuple[str, str, dict]:
    """One DashScope omni call (streaming, base64 video). Returns (text, error, usage)."""
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": video_data_url(video_path)}},
                {"type": "text", "text": prompt},
            ],
        }],
        "modalities": ["text"],
        "max_tokens": 8000,
        "response_format": {"type": "json_object"},
    }
    result, err, usage = post_chat(QWEN_BASE, key, payload, stream=True)
    if err and "response_format" in err:
        payload.pop("response_format")
        result, err, usage = post_chat(QWEN_BASE, key, payload, stream=True)
    return (result or {}).get("content", ""), err, usage






def kimi_upload_video(key: str, video_path: str) -> tuple[str, str]:
    """Upload a video to the Moonshot files API; return (file_id, error).

    The documented purpose for K3 video understanding is file-based; probe a
    couple of purpose values and report what the server says.
    """
    headers = {"Authorization": f"Bearer {key}"}
    errors = []
    for purpose in ("video", "file-extract"):
        with open(video_path, "rb") as fh:
            r = requests.post(
                f"{KIMI_BASE}/files", headers=headers,
                files={"file": (Path(video_path).name, fh, "video/mp4")},
                data={"purpose": purpose}, timeout=TIMEOUT,
            )
        if r.status_code == 200:
            return r.json()["id"], ""
        errors.append(f"purpose={purpose}: HTTP {r.status_code}: {r.text[:500]}")
    return "", " | ".join(errors)






def call_kimi(key: str, model: str, prompt: str, video_path: str,
              schema: dict) -> tuple[str, str, dict, list[str]]:
    """One Kimi call, probing video transports + response_format tiers.

    Attempts, in order: base64 data-URL video, then file-upload (ms:// ref);
    for each, response_format json_schema → json_object → none. Returns
    (text, error, usage, notes) where notes records which combination worked.
    """
    notes: list[str] = []
    transports: list[tuple[str, str]] = [("base64", video_data_url(video_path))]
    file_id, up_err = kimi_upload_video(key, video_path)
    if file_id:
        transports.append(("ms_file", f"ms://{file_id}"))
    else:
        notes.append(f"file upload failed: {up_err}")
    formats: list[tuple[str, dict | None]] = [
        ("json_schema", {"type": "json_schema",
                         "json_schema": {"name": "annotation", "schema": schema}}),
        ("json_object", {"type": "json_object"}),
        ("none", None),
    ]
    last_err = ""
    for t_name, url in transports:
        for f_name, rf in formats:
            payload = {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "video_url", "video_url": {"url": url}},
                        {"type": "text", "text": prompt},
                    ],
                }],
            }
            if rf:
                payload["response_format"] = rf
            result, err, usage = post_chat(KIMI_BASE, key, payload, stream=False)
            if not err:
                notes.append(f"worked: transport={t_name} response_format={f_name}")
                return result["content"], "", usage, notes
            notes.append(f"transport={t_name} response_format={f_name} -> {err[:300]}")
            last_err = err
    return "", last_err, {}, notes






def word_overlap(a: str, b: str) -> float:
    """Crude bag-of-words overlap between two transcripts (0..1)."""
    wa = set(re.findall(r"[a-z']+", (a or "").lower()))
    wb = set(re.findall(r"[a-z']+", (b or "").lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)






def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--providers", default="qwen,kimi")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--qwen-model", default="qwen3-omni-flash")
    ap.add_argument("--kimi-model", default="kimi-k3")
    ap.add_argument("--workdir", default=os.path.expanduser("~/qwen_eval_work"))
    args = ap.parse_args()

    workdir = Path(args.workdir)
    contract_prompt, schema, items, reference = load_fixtures(workdir, args.n)
    prompt = build_prompt(contract_prompt, schema)
    required = set(schema.get("properties", {}))
    providers = [p.strip() for p in args.providers.split(",") if p.strip()]

    keys = {"qwen": os.environ.get("DASHSCOPE_API_KEY", ""),
            "kimi": os.environ.get("MOONSHOT_API_KEY", "")}
    missing = [p for p in providers if not keys.get(p)]
    if missing:
        names = {"qwen": "DASHSCOPE_API_KEY", "kimi": "MOONSHOT_API_KEY"}
        raise SystemExit("Missing API keys: " + ", ".join(names[p] for p in missing))

    print(f"Testing {len(items)} items x {providers} "
          f"(qwen={args.qwen_model}, kimi={args.kimi_model})\n")
    results: dict[str, dict] = {}
    for item in items:
        iid = item["item_id"]
        ref = (reference.get(iid) or {}).get("parsed") or reference.get(iid) or {}
        size_mb = Path(item["video_file"]).stat().st_size / 1e6
        print(f"=== {iid}  ({item['duration']:.0f}s, {size_mb:.1f} MB) ===")
        for prov in providers:
            t0 = time.time()
            notes: list[str] = []
            if prov == "qwen":
                text, err, usage = call_qwen(keys["qwen"], args.qwen_model,
                                             prompt, item["video_file"])
            else:
                text, err, usage, notes = call_kimi(keys["kimi"], args.kimi_model,
                                                    prompt, item["video_file"], schema)
            dt = time.time() - t0
            parsed = parse_json_response(text) if text else None
            row = {"error": err, "latency_s": round(dt, 1), "usage": usage,
                   "notes": notes, "raw": text}
            if parsed is not None:
                miss = sorted(required - set(parsed))
                extra = sorted(set(parsed) - required)
                overlap = word_overlap(parsed.get("transcript", ""),
                                       ref.get("transcript", ""))
                row |= {"parsed_ok": True, "missing_keys": miss, "extra_keys": extra,
                        "transcript_overlap_vs_gemini": round(overlap, 2)}
                print(f"  {prov:5s} OK   {dt:6.1f}s  tokens={usage.get('prompt_tokens')}"
                      f"/{usage.get('completion_tokens')}  "
                      f"missing={miss or '-'} extra={extra or '-'}  "
                      f"transcript-overlap={overlap:.2f}")
                print(f"        transcript: {parsed.get('transcript', '')[:110]!r}")
            else:
                row["parsed_ok"] = False
                print(f"  {prov:5s} FAIL {dt:6.1f}s  "
                      f"{err[:200] if err else 'unparseable output: ' + text[:200]!r}")
            for note in notes:
                print(f"        note: {note}")
            results.setdefault(iid, {})[prov] = row
        print()

    out = workdir / "api_smoke_results.json"
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(f"Raw results saved to {out}")






if __name__ == "__main__":
    main()
