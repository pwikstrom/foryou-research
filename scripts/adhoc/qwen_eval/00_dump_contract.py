"""Dump the production annotation prompt + portable JSON schema to the workdir.

Lets the Qwen annotation step (02) run in its own mlx venv without importing fyp.

    FYP_FORCE_GCS=1 FYP_GCS_BUCKET_NAME=fyp_bucket_01 \
        PYTHONPATH=. .venv/bin/python scripts/adhoc/qwen_eval/00_dump_contract.py
"""

import argparse
import json
import os

import fyp.annotation_schema as sch


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=os.path.expanduser("~/qwen_eval_work"))
    args = ap.parse_args()
    os.makedirs(args.workdir, exist_ok=True)

    prompt = sch.build_prompt()
    schema = sch.get_annotation_json_schema()

    with open(os.path.join(args.workdir, "prompt.txt"), "w") as f:
        f.write(prompt)
    with open(os.path.join(args.workdir, "response_schema.json"), "w") as f:
        json.dump(schema, f, indent=1)
    print(f"prompt ({len(prompt)} chars) and schema "
          f"({len(schema.get('properties', {}))} fields) written to {args.workdir}")






if __name__ == "__main__":
    main()
