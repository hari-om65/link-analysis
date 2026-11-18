"""Call the running API and save real request/response samples into docs/samples/.

Start the server first:
    uvicorn app.main:app --host 0.0.0.0 --port 4000

Then:
    python -m scripts.capture_samples
    python -m scripts.capture_samples --base-url https://your-app.onrender.com --urls https://basecamp.com
"""

import argparse
import json
import re
import time
from pathlib import Path

import httpx

OUT_DIR = Path("docs/samples")

SUCCESS_URLS = ["https://basecamp.com"]
ERROR_CASES = {
    "error-invalid-url": "not a url",
    "error-private-ip": "http://192.168.1.1",
    "error-cloud-metadata": "http://169.254.169.254/latest/meta-data/",
    "error-localhost": "http://localhost",
    "error-unknown-domain": "https://this-domain-does-not-exist-4821.com",
}


def slug(url: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", url.lower().split("://")[-1]).strip("-")[:60]


def call(client: httpx.Client, base_url: str, url: str) -> tuple[int, dict, float]:
    started = time.perf_counter()
    response = client.post(f"{base_url}/link-analysis", json={"url": url})
    return response.status_code, response.json(), round(time.perf_counter() - started, 2)


def main(base_url: str, urls: list[str], with_errors: bool) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=180) as client:
        cases = {f"success-{slug(u)}": u for u in urls}
        if with_errors:
            cases.update(ERROR_CASES)
        for name, url in cases.items():
            status, body, seconds = call(client, base_url, url)
            sample = {
                "request": {"method": "POST", "path": "/link-analysis", "body": {"url": url}},
                "status_code": status,
                "response_time_seconds": seconds,
                "response": body,
            }
            path = OUT_DIR / f"{name}.json"
            path.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"{status}  {seconds:>6}s  {url}  ->  {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:4000")
    parser.add_argument("--urls", nargs="*", default=SUCCESS_URLS)
    parser.add_argument("--no-errors", action="store_true", help="skip the error-case samples")
    args = parser.parse_args()
    main(args.base_url.rstrip("/"), args.urls, not args.no_errors)
