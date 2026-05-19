#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.request


DEFAULT_MODELS = [
    "mistral-nemo:12b",
    "qwen2.5-coder:7b",
    "qwen2.5:7b",
    "llama3.2:latest",
]

SYSTEM_PROMPT = """You are a cautious hard-drive recovery advisor.
The user is recovering files from a large failing disk without enough local space for a full clone.
Your job is to provide safe next-step guidance only. You do not execute commands.

Rules:
- Prefer read-only inspection and read-only mounts.
- Never mount the source drive on the recovery output directory.
- Use a separate source mount point such as /mnt/recovery_source or /tmp/recovery_source_mount.
- Treat the recovery output directory only as the copy destination.
- Never suggest formatting, fsck repair, partition edits, destructive writes, or full-disk cloning.
- Keep answers short and operational.
- When suggesting commands, label them as suggestions and favor: lsblk, blkid, smartctl, mount -o ro, find, rsync, cp, df, du.
- If there is risk, say what to verify before proceeding.
"""


def split_models(value: str) -> list[str]:
    models = [item.strip() for item in value.split(",") if item.strip()]
    return models or DEFAULT_MODELS


def ask_ollama(prompt: str, model: str, host: str) -> str:
    payload = json.dumps({
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0.1},
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result["message"]["content"].strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe local hard-drive recovery advisor.")
    parser.add_argument("--prompt", required=True, help="Recovery question or context.")
    parser.add_argument(
        "--models",
        default=os.environ.get("RECOVERY_ADVISOR_MODELS", ",".join(DEFAULT_MODELS)),
        help="Comma-separated Ollama models to try in order.",
    )
    parser.add_argument(
        "--ollama-host",
        default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
        help="Ollama host URL.",
    )
    args = parser.parse_args()

    errors = []
    for model in split_models(args.models):
        try:
            answer = ask_ollama(args.prompt, model, args.ollama_host)
            print(f"[model: {model}]")
            print(answer)
            return 0
        except Exception as exc:
            errors.append(f"{model}: {exc}")

    print("All advisor models failed:", file=sys.stderr)
    for error in errors:
        print(f"- {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
