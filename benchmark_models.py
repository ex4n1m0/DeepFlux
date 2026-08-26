"""Benchmark OpenRouter models for DeepFlux agent suitability.

Tests each model against representative agent tasks (torrent search,
direct download discovery, tool-use reasoning) and reports latency,
token cost, and qualitative scores.

Usage:
    set OPENROUTER_KEY=sk-or-v1-...
    python benchmark_models.py [--quick] [--model MODEL_ID]
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Model list — mirrors config.py OpenRouter preset
# ---------------------------------------------------------------------------
MODELS = [
    # DeepSeek
    "deepseek/deepseek-v4-pro-0813",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash-0731",
    "deepseek/deepseek-v4-flash",
    # Anthropic
    "anthropic/claude-sonnet-4",
    # OpenAI
    "openai/gpt-5.6-luna-pro",
    "openai/gpt-5.6-luna",
    "openai/gpt-4.1",
    "openai/gpt-4o-mini",
    # Google
    "google/gemini-3.6-flash",
    "google/gemini-2.5-pro",
    # Meta
    "meta-llama/llama-4-maverick",
    # xAI
    "x-ai/grok-4.3",
    # Mistral
    "mistralai/mistral-medium-3-5",
]

BASE_URL = "https://openrouter.ai/api/v1"

# ---------------------------------------------------------------------------
# Test prompts — representative agent scenarios
# ---------------------------------------------------------------------------
TEST_PROMPTS = [
    {
        "id": "torrent_search",
        "description": "Find torrents for a popular movie",
        "system": (
            "You are DeepFlux, a torrent download assistant. You help users find "
            "and download content via torrents and direct downloads. You have access "
            "to search tools and can reason about results. Be concise and actionable."
        ),
        "user": (
            "I want to download the movie Inception (2010) in 1080p quality. "
            "What are the best approaches to find it? List specific search strategies "
            "I should use — which torrent sites, what search queries, and what to "
            "look for in results (seeders, file size, video codec). Also mention "
            "direct download alternatives if torrents are unavailable."
        ),
        "eval_criteria": [
            "Mentions specific torrent indexers/sites",
            "Suggests effective search queries",
            "Advises on seeder/file size/quality heuristics",
            "Mentions direct download fallbacks",
            "Structured, actionable response",
        ],
    },
    {
        "id": "direct_download",
        "description": "Find direct download links for software",
        "system": (
            "You are DeepFlux, a download assistant. You help users find direct "
            "download links for software, media, and other content. You understand "
            "HLS/DASH streaming, file hosting sites, and how to extract download "
            "URLs from web pages."
        ),
        "user": (
            "I need to download the latest version of Blender (3D modeling software). "
            "How would you find a direct download link? Walk me through the search "
            "strategy — which sites to check, how to verify the download is legitimate, "
            "and what file hosting patterns to look for."
        ),
        "eval_criteria": [
            "Identifies official sources first",
            "Suggests mirror/hosting sites",
            "Advises on checksum/verification",
            "Mentions file extension patterns",
            "Practical, step-by-step guidance",
        ],
    },
    {
        "id": "tool_selection",
        "description": "Choose the right tool for a task",
        "system": (
            "You are DeepFlux, a torrent and download assistant. You have these tools:\n"
            "- search_indexers(query, category, deep): search Jackett-connected torrent indexers\n"
            "- web_search(query): search the web via DuckDuckGo/Brave/Perplexity\n"
            "- web_fetch(url): fetch and extract magnets, torrent files, download links from a page\n"
            "- add_magnet(uri, category, save_path): add a magnet link to the download queue\n"
            "- add_download(url, filename, category): queue a direct HTTP/stream download\n"
            "- list_torrents(): show current downloads\n\n"
            "Choose the right tool(s) for each request and explain your reasoning."
        ),
        "user": (
            "A user says: 'I found this page https://example.com/linux-isos that "
            "lists several torrent magnet links and direct download URLs. Grab "
            "everything that looks like a Linux ISO.'\n\n"
            "Which tools would you use, in what order, and why?"
        ),
        "eval_criteria": [
            "Correctly identifies web_fetch as first step",
            "Understands magnet vs direct download extraction",
            "Mentions filtering by filename/type",
            "Correct tool sequencing (fetch → filter → add)",
            "Mentions confirmation for destructive actions",
        ],
    },
]

# Quick mode: only the tool_selection prompt (most discriminative)
QUICK_PROMPTS = ["tool_selection"]


@dataclass
class BenchResult:
    model: str
    prompt_id: str
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    content: str = ""
    error: str = ""
    scores: Dict[str, int] = field(default_factory=dict)


def call_openrouter(
    api_key: str,
    model: str,
    system: str,
    user: str,
    timeout: int = 60,
) -> dict:
    """Single non-streaming chat completion via OpenRouter."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/deeptorrent",
        "X-Title": "DeepFlux Model Benchmark",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    resp = requests.post(
        f"{BASE_URL}/chat/completions",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def evaluate_response(content: str, criteria: List[str]) -> Dict[str, int]:
    """Quick heuristic evaluation — counts keyword presence per criterion."""
    keyword_map = {
        # torrent_search criteria
        "Mentions specific torrent indexers/sites": [
            "1337x", "rarbg", "torrentgalaxy", "yts", "eztv", "pirate bay",
            "thepiratebay", "torrent", "indexer", "jackett", "prowlarr",
            "nyaa", "rutracker", "limetorrent", "kickass",
        ],
        "Suggests effective search queries": [
            "inception 2010 1080p", "search query", "search for", "query",
            "bluray", "x264", "x265", "hevc",
        ],
        "Advises on seeder/file size/quality heuristics": [
            "seeder", "seed", "leech", "file size", "gb", "mb",
            "quality", "resolution", "bitrate", "codec",
        ],
        "Mentions direct download fallbacks": [
            "direct download", "ddl", "mega", "google drive", "mediafire",
            "http", "stream", "hls", "dash",
        ],
        "Structured, actionable response": [
            "1.", "2.", "3.", "first", "second", "finally",
            "step", "- ", "* ",
        ],
        # direct_download criteria
        "Identifies official sources first": [
            "official", "blender.org", "website", "download page",
            "github", "source",
        ],
        "Suggests mirror/hosting sites": [
            "mirror", "fosshub", "filehorse", "softpedia", "majorgeeks",
            "alternative", "hosting",
        ],
        "Advises on checksum/verification": [
            "checksum", "sha256", "md5", "hash", "verify", "signature",
            "gpg", "authentic",
        ],
        "Mentions file extension patterns": [
            ".exe", ".msi", ".dmg", ".zip", ".tar", ".appimage",
            "extension", "installer", "portable",
        ],
        "Practical, step-by-step guidance": [
            "1.", "2.", "3.", "first", "second", "finally",
            "step", "navigate", "click", "select",
        ],
        # tool_selection criteria
        "Correctly identifies web_fetch as first step": [
            "web_fetch", "fetch", "scrape", "extract", "page",
        ],
        "Understands magnet vs direct download extraction": [
            "magnet", "direct download", "separate", "distinguish",
            "both", "two types",
        ],
        "Mentions filtering by filename/type": [
            "filter", ".iso", "linux", "filename", "extension",
            "match", "pattern",
        ],
        "Correct tool sequencing (fetch → filter → add)": [
            "then", "after", "next", "finally", "sequence",
            "order", "step",
        ],
        "Mentions confirmation for destructive actions": [
            "confirm", "ask", "verify", "destructive", "review",
            "check", "before",
        ],
    }

    content_lower = content.lower()
    scores = {}
    for criterion in criteria:
        keywords = keyword_map.get(criterion, [])
        hits = sum(1 for kw in keywords if kw.lower() in content_lower)
        # Score 0-3 based on keyword coverage
        if not keywords:
            scores[criterion] = 1
        elif hits >= 3:
            scores[criterion] = 3
        elif hits >= 1:
            scores[criterion] = 2
        else:
            scores[criterion] = 0
    return scores


def run_benchmark(
    api_key: str,
    models: List[str],
    prompts: List[dict],
) -> List[BenchResult]:
    """Run all models against all prompts."""
    results: List[BenchResult] = []
    total = len(models) * len(prompts)

    for i, model in enumerate(models):
        for j, prompt in enumerate(prompts):
            idx = i * len(prompts) + j + 1
            print(f"\n[{idx}/{total}] {model} :: {prompt['id']}", flush=True)

            result = BenchResult(model=model, prompt_id=prompt["id"])

            try:
                t0 = time.perf_counter()
                resp = call_openrouter(
                    api_key, model, prompt["system"], prompt["user"]
                )
                t1 = time.perf_counter()
                result.duration_ms = int((t1 - t0) * 1000)

                choice = resp["choices"][0]
                result.content = choice["message"].get("content", "") or ""

                usage = resp.get("usage", {})
                result.prompt_tokens = usage.get("prompt_tokens", 0)
                result.completion_tokens = usage.get("completion_tokens", 0)
                result.total_tokens = usage.get("total_tokens", 0)

                # Estimate cost from OpenRouter pricing headers or defaults
                pricing = resp.get("usage", {})
                result.cost_usd = float(pricing.get("cost", 0))

                # Evaluate quality
                result.scores = evaluate_response(
                    result.content, prompt["eval_criteria"]
                )

                total_score = sum(result.scores.values())
                max_score = len(prompt["eval_criteria"]) * 3
                print(
                    f"  {result.duration_ms}ms | "
                    f"{result.total_tokens}t | "
                    f"${result.cost_usd:.6f} | "
                    f"score {total_score}/{max_score}",
                    flush=True,
                )

            except Exception as exc:
                result.error = str(exc)
                print(f"  ERROR: {exc}", flush=True)

            results.append(result)

    return results


def print_report(results: List[BenchResult]) -> None:
    """Print a summary comparison table."""
    # Group by model
    by_model: Dict[str, List[BenchResult]] = {}
    for r in results:
        by_model.setdefault(r.model, []).append(r)

    print("\n" + "=" * 120)
    print("MODEL BENCHMARK REPORT")
    print("=" * 120)

    # Header
    header = (
        f"{'Model':<42} {'Avg ms':>8} {'Avg tok':>8} "
        f"{'Cost $':>10} {'Score':>6} {'Errors':>6}"
    )
    print(header)
    print("-" * 120)

    # Per-model averages
    rows = []
    for model, model_results in by_model.items():
        valid = [r for r in model_results if not r.error]
        errors = len([r for r in model_results if r.error])

        if not valid:
            rows.append((model, 0, 0, 0.0, 0, errors))
            continue

        avg_ms = sum(r.duration_ms for r in valid) // len(valid)
        avg_tok = sum(r.total_tokens for r in valid) // len(valid)
        total_cost = sum(r.cost_usd for r in valid)
        total_score = sum(sum(r.scores.values()) for r in valid)
        max_score = sum(len(r.scores) for r in valid) * 3
        score_pct = (total_score / max_score * 100) if max_score else 0

        rows.append((model, avg_ms, avg_tok, total_cost, score_pct, errors))

    # Sort by score desc
    rows.sort(key=lambda x: x[4], reverse=True)

    for model, avg_ms, avg_tok, cost, score_pct, errors in rows:
        err_str = str(errors) if errors else ""
        print(
            f"{model:<42} {avg_ms:>7}ms {avg_tok:>7}t "
            f"${cost:>9.6f} {score_pct:>5.0f}% {err_str:>6}"
        )

    print("-" * 120)

    # Per-prompt breakdown
    print("\n--- Per-Prompt Breakdown ---")
    prompt_ids = sorted({r.prompt_id for r in results})
    for pid in prompt_ids:
        print(f"\n  {pid}:")
        prompt_results = [r for r in results if r.prompt_id == pid and not r.error]
        prompt_results.sort(
            key=lambda r: sum(r.scores.values()), reverse=True
        )
        for r in prompt_results[:5]:  # top 5 per prompt
            total = sum(r.scores.values())
            max_s = len(r.scores) * 3
            print(
                f"    {r.model:<44} {r.duration_ms:>6}ms  "
                f"${r.cost_usd:.6f}  score {total}/{max_s}"
            )

    print("\n" + "=" * 120)
    print("RECOMMENDATIONS")
    print("=" * 120)

    if rows:
        best = rows[0]
        print(f"  Best overall:    {best[0]} (score {best[4]:.0f}%)")

        # Fast model candidates: score >= 60% AND fastest among those
        fast_candidates = [r for r in rows if r[4] >= 50]
        fast_candidates.sort(key=lambda x: x[1])  # sort by speed
        if fast_candidates:
            fast = fast_candidates[0]
            print(f"  Best fast model: {fast[0]} ({fast[1]}ms avg, score {fast[4]:.0f}%)")

        # Best value: score >= 60% AND cheapest
        value_candidates = [r for r in rows if r[4] >= 50]
        value_candidates.sort(key=lambda x: x[3])  # sort by cost
        if value_candidates:
            value = value_candidates[0]
            print(f"  Best value:      {value[0]} (${value[3]:.6f}, score {value[4]:.0f}%)")


def main() -> None:
    api_key = os.environ.get("OPENROUTER_KEY", "")
    if not api_key:
        print("Set OPENROUTER_KEY environment variable", file=sys.stderr)
        sys.exit(1)

    quick = "--quick" in sys.argv
    model_filter = None
    for arg in sys.argv[1:]:
        if arg.startswith("--model="):
            model_filter = arg.split("=", 1)[1]

    models = [model_filter] if model_filter else MODELS
    prompts = [
        p for p in TEST_PROMPTS
        if not quick or p["id"] in QUICK_PROMPTS
    ]

    print(f"Benchmarking {len(models)} model(s) x {len(prompts)} prompt(s)")
    print(f"Quick mode: {quick}")
    print(f"Models: {models}")

    results = run_benchmark(api_key, models, prompts)
    print_report(results)

    # Save raw results
    out_path = "benchmark_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            [
                {
                    "model": r.model,
                    "prompt_id": r.prompt_id,
                    "duration_ms": r.duration_ms,
                    "prompt_tokens": r.prompt_tokens,
                    "completion_tokens": r.completion_tokens,
                    "total_tokens": r.total_tokens,
                    "cost_usd": r.cost_usd,
                    "content": r.content[:500],
                    "error": r.error,
                    "scores": r.scores,
                }
                for r in results
            ],
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"\nRaw results saved to {out_path}")


if __name__ == "__main__":
    main()
