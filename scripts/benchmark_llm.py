"""Benchmark several LLMs on the same website content.

Each website is fetched once, then the exact same prompt is sent to every
model so the comparison is fair. Results (latency, tokens, cost, whether the
JSON passed validation) are written to docs/benchmark/.

Only models whose API key is present in .env are run.

Usage:
    python -m scripts.benchmark_llm
    python -m scripts.benchmark_llm --urls https://basecamp.com https://www.zoho.com/crm/ --runs 2
    python -m scripts.benchmark_llm --models gemini:gemini-3.5-flash-lite openai:gpt-5-mini
"""

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import estimate_cost, get_settings
from app.exceptions import AppError
from app.services.analysis_service import AnalysisService
from app.services.competitor_service import CompetitorService
from app.services.llm_service import build_client
from app.services.prompts import build_user_prompt
from app.services.website_extractor import WebsiteExtractor

DEFAULT_URLS = ["https://basecamp.com", "https://www.freshworks.com", "https://www.canva.com"]
DEFAULT_MODELS = [
    "gemini:gemini-3.5-flash-lite",
    "gemini:gemini-3.1-flash-lite",
    "gemini:gemini-2.5-flash-lite",
    "gemini:gemini-3-flash-preview",
    "openai:gpt-5-mini",
    "openai:gpt-5-nano",
    "openai:gpt-4.1-mini",
    "groq:openai/gpt-oss-120b",
]
OUT_DIR = Path("docs/benchmark")


def has_key(provider: str, settings) -> bool:
    return bool(getattr(settings, f"{provider}_api_key", None))


async def run(urls: list[str], models: list[str], runs: int) -> None:
    settings = get_settings()
    selected = [m for m in models if has_key(m.split(":", 1)[0], settings)]
    skipped = sorted(set(models) - set(selected))
    if skipped:
        print("Skipping (no API key):", ", ".join(skipped))
    if not selected:
        print("No models to run. Add at least one API key to .env")
        return

    rows = []
    async with httpx.AsyncClient(headers={"User-Agent": settings.user_agent}, timeout=settings.fetch_timeout_seconds) as http:
        extractor = WebsiteExtractor(http, settings)
        for url in urls:
            try:
                content = await extractor.extract(url)
            except AppError as exc:
                print(f"[{url}] could not extract: {exc.message}")
                continue
            prompt = build_user_prompt(content.to_prompt(settings.max_content_chars), content.seo_audit.issues)
            print(f"\n[{url}] prompt is {len(prompt):,} chars")

            for spec in selected:
                provider, model = spec.split(":", 1)
                client = build_client(provider, model, http, settings)
                service = AnalysisService(extractor, CompetitorService(http, verify_sites=False), [client], settings)
                for attempt in range(runs):
                    started = time.perf_counter()
                    row = {"url": url, "provider": provider, "model": model, "run": attempt + 1}
                    try:
                        output, usage = await service._generate_validated(client, prompt)
                        row.update(
                            ok=True,
                            seconds=round(time.perf_counter() - started, 2),
                            llm_calls=usage.calls,
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            cost_usd=estimate_cost(model, usage.input_tokens, usage.output_tokens),
                            brand=output.brand.name,
                            competitors=[c.name for c in output.analysis.competitor_analysis.competitors],
                            services=[s.service_name for s in output.analysis.top_services],
                            primary_keyword=output.analysis.seo_improvements.primary_keyword,
                        )
                    except AppError as exc:
                        row.update(ok=False, seconds=round(time.perf_counter() - started, 2), error=exc.message[:200])
                    rows.append(row)
                    status = "ok " if row["ok"] else "ERR"
                    print(f"  {status} {spec:<34} {row['seconds']:>6}s  {row.get('cost_usd', '')}")

    write_report(rows)


def write_report(rows: list[dict]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    (OUT_DIR / "results.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    by_model: dict[str, list[dict]] = {}
    for row in rows:
        by_model.setdefault(f"{row['provider']}:{row['model']}", []).append(row)

    lines = [
        f"# LLM benchmark results ({stamp})",
        "",
        "Measured with `scripts/benchmark_llm.py` - same extracted content and prompt for every model.",
        "",
        "| Model | Runs | Valid JSON | Median time (s) | Avg input tokens | Avg output tokens | Avg cost (USD) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, items in by_model.items():
        good = [r for r in items if r["ok"]]
        median = statistics.median(r["seconds"] for r in good) if good else None
        avg = lambda key: round(statistics.mean(r[key] for r in good)) if good else None  # noqa: E731
        costs = [r["cost_usd"] for r in good if r.get("cost_usd") is not None]
        lines.append(
            f"| {name} | {len(items)} | {len(good)}/{len(items)} | {median if median is not None else '-'} | "
            f"{avg('input_tokens') or '-'} | {avg('output_tokens') or '-'} | "
            f"{round(statistics.mean(costs), 5) if costs else '-'} |"
        )

    lines += ["", "## Output spot-check", ""]
    for row in rows:
        if row["ok"]:
            lines.append(
                f"- **{row['model']}** on {row['url']}: brand `{row['brand']}`, keyword `{row['primary_keyword']}`, "
                f"competitors {', '.join(row['competitors'])}"
            )
        else:
            lines.append(f"- **{row['model']}** on {row['url']}: failed - {row['error']}")

    (OUT_DIR / "results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nSaved {OUT_DIR / 'results.md'} and results.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare LLMs for website link analysis")
    parser.add_argument("--urls", nargs="+", default=DEFAULT_URLS)
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="provider:model")
    parser.add_argument("--runs", type=int, default=1, help="runs per model per url")
    args = parser.parse_args()
    asyncio.run(run(args.urls, args.models, args.runs))
