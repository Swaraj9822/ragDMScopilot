"""Probe: does this environment have access to the Vertex AI Ranking model
`semantic-ranker-default-004`?

Uses Application Default Credentials (ADC) and the same GCP project/location the
RAG system is configured with, then issues a minimal RankService.rank request.
Prints a clear verdict without leaking credentials.
"""

from __future__ import annotations

import os
import sys

import google.auth
from google.auth.transport.requests import AuthorizedSession

MODEL = "semantic-ranker-default-004"


def _load_env_from_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so we reuse the project's GCP_PROJECT_ID etc."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def main() -> int:
    _load_env_from_dotenv()

    # The Ranking API is NOT available in the "global" multi-region the way the
    # Gemini generation endpoint is; ranking configs live under a regional or
    # the "global" collection location. We try the configured location first,
    # falling back to "global".
    configured_location = os.environ.get("GCP_LOCATION", "global") or "global"

    try:
        creds, adc_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] Could not obtain Application Default Credentials: {exc}")
        print("       Configure ADC (gcloud auth application-default login) or set")
        print("       GOOGLE_APPLICATION_CREDENTIALS to a service-account key file.")
        return 2

    project = os.environ.get("GCP_PROJECT_ID") or adc_project
    if not project:
        print("[FAIL] No GCP project resolved from env or ADC.")
        return 2

    print(f"Project:  {project}")
    print(f"Location: {configured_location}  (will also try 'global')")
    print(f"Model:    {MODEL}")
    print("-" * 60)

    session = AuthorizedSession(creds)

    locations = []
    for loc in (configured_location, "global"):
        if loc not in locations:
            locations.append(loc)

    payload = {
        "model": MODEL,
        "query": "What is the capital of France?",
        "records": [
            {"id": "1", "title": "France", "content": "Paris is the capital of France."},
            {"id": "2", "title": "Germany", "content": "Berlin is the capital of Germany."},
        ],
        "topN": 2,
    }

    for loc in locations:
        url = (
            f"https://discoveryengine.googleapis.com/v1/projects/{project}"
            f"/locations/{loc}/rankingConfigs/default_ranking_config:rank"
        )
        print(f"\n>>> POST {url}")
        try:
            resp = session.post(url, json=payload, timeout=60)
        except Exception as exc:  # noqa: BLE001
            print(f"    [ERROR] request failed: {exc}")
            continue

        print(f"    HTTP {resp.status_code}")
        body = resp.text
        if resp.status_code == 200:
            print("    [OK] Access granted — ranker returned a response:")
            print("    " + body[:800])
            print(f"\n[VERDICT] YES — you have access to '{MODEL}' (location='{loc}').")
            return 0

        # Non-200: surface the error so we can distinguish the failure mode.
        print("    " + body[:800])

    print(f"\n[VERDICT] Could not confirm access to '{MODEL}'. See errors above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
