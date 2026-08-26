# DeepFlux Model Benchmark Report

**Date:** August 13, 2026  
**API:** OpenRouter  
**Prompts:** 3 agent-style scenarios (torrent search, direct download discovery, tool selection)

---

## Executive Summary

Tested 14 OpenRouter models against 3 representative DeepFlux agent tasks. **7 models are inaccessible** with this API key (403 Forbidden on Anthropic, OpenAI, Google providers). Of the 7 working models, **DeepSeek dominates**: all 4 DeepSeek variants scored 93%+ with excellent cost efficiency.

### Top Recommendations

| Role | Model | Score | Avg Time | Total Cost | Why |
|---|---|---|---|---|---|
| **Main Agent** | `deepseek/deepseek-v4-pro` | 93% | 24.9s | $0.0042 | Best quality/speed/cost balance |
| **Fast Model** | `deepseek/deepseek-v4-flash` | 93% | 23.3s | $0.0003 | Virtually free, same score as Pro |
| **Speed King** | `mistralai/mistral-medium-3-5` | 96% | 7.8s | $0.0230 | Fastest, highest quality, but 78x pricier than flash |
| **Budget Pick** | `deepseek/deepseek-v4-flash` | 93% | 23.3s | $0.0003 | $0.000082 per tool_selection call |

---

## Full Results

### Working Models (7/14)

| # | Model | Torrent Search | Direct Download | Tool Selection | **Avg Score** | **Avg Time** | **Total Cost** |
|---|---|---|---|---|---|---|---|
| 1 | `deepseek/deepseek-v4-flash-0731` | 15/15 | 15/15 | 14/15 | **98%** | 52.7s | $0.00079 |
| 2 | `meta-llama/llama-4-maverick` | 15/15 | 14/15 | 14/15 | **96%** | 29.6s | $0.00123 |
| 3 | `mistralai/mistral-medium-3-5` | 15/15 | 14/15 | 14/15 | **96%** | 7.8s | $0.02298 |
| 4 | `deepseek/deepseek-v4-pro-0813` | 15/15 | 14/15 | 13/15 | **93%** | 45.5s | $0.03019 |
| 5 | `deepseek/deepseek-v4-pro` | 15/15 | 14/15 | 13/15 | **93%** | 24.9s | $0.00416 |
| 6 | `deepseek/deepseek-v4-flash` | 15/15 | 14/15 | 13/15 | **93%** | 23.3s | $0.00030 |
| 7 | `x-ai/grok-4.3` | 8/15 | 15/15 | 11/15 | **76%** | 7.3s | $0.00552 |

### Inaccessible Models (7/14) — 403 Forbidden

These models returned HTTP 403 — the API key does not have access to these providers:

- `anthropic/claude-sonnet-4`
- `openai/gpt-5.6-luna-pro`
- `openai/gpt-5.6-luna`
- `openai/gpt-4.1`
- `openai/gpt-4o-mini`
- `google/gemini-3.6-flash`
- `google/gemini-2.5-pro`

> **Note:** To use Anthropic, OpenAI, or Google models through OpenRouter, you need to add credits to your OpenRouter account or check provider settings at https://openrouter.ai/settings.

---

## Per-Prompt Analysis

### Torrent Search ("Find Inception 2010 in 1080p")

| Model | Time | Cost | Score | Notes |
|---|---|---|---|---|
| `deepseek-v4-flash` | 38.0s | $0.00009 | 15/15 | Cheapest perfect score — specific site names, query examples, seed heuristics |
| `llama-4-maverick` | 8.5s | $0.00040 | 15/15 | Fastest perfect score — concise but comprehensive |
| `mistral-medium-3-5` | 9.7s | $0.00911 | 15/15 | Fast, perfect, but 100x cost of flash |
| `grok-4.3` | 5.3s | $0.00125 | 8/15 | Fastest but missed key criteria — too brief, no seed heuristics |
| `v4-pro-0813` | 41.6s | $0.00900 | 15/15 | Most thorough — disclaimer, table format, file size ranges |

### Direct Download ("Download latest Blender")

| Model | Time | Cost | Score | Notes |
|---|---|---|---|
| `v4-flash-0731` | 57.7s | $0.00030 | 15/15 | Perfect — golden rule (official first), mirror patterns, GPG verification |
| `grok-4.3` | 10.1s | $0.00222 | 15/15 | Fastest perfect — surprisingly good on this prompt |
| `v4-pro` | 20.7s | $0.00098 | 14/15 | Solid — official source + mirror patterns, missed some hosting sites |
| `v4-flash` | 22.8s | $0.00012 | 14/15 | Good value — official first, mirror suggestions, checksums |

### Tool Selection ("Grab Linux ISOs from a page")

| Model | Time | Cost | Score | Notes |
|---|---|---|---|
| `v4-flash-0731` | 9.6s | $0.00013 | 14/15 | Best — correct sequence, filtering, confirmation mention |
| `llama-4-maverick` | 70.2s | $0.00031 | 14/15 | Good but very slow on this prompt |
| `mistral-medium-3-5` | 4.7s | $0.00436 | 14/15 | Fastest — concise correct sequence |
| `v4-flash` | 9.2s | $0.00008 | 13/15 | Cheapest — correct but missed confirmation nuance |

---

## Cost Analysis

### Per-Call Cost (Single Tool Selection Prompt)

| Model | Cost per Call |
|---|---|
| `deepseek-v4-flash` | **$0.000082** |
| `deepseek-v4-flash-0731` | $0.000130 |
| `deepseek-v4-pro` | $0.000961 |
| `llama-4-maverick` | $0.000314 |
| `grok-4.3` | $0.002042 |
| `mistral-medium-3-5` | $0.004360 |
| `deepseek-v4-pro-0813` | $0.005883 |

### Estimated Monthly Cost (100 agent interactions/day, avg 3 LLM calls each)

| Model | Daily Cost | Monthly Cost |
|---|---|---|
| `deepseek-v4-flash` (main + fast) | $0.03 | **$0.90** |
| `deepseek-v4-pro` (main) + `v4-flash` (fast) | $0.32 | **$9.60** |
| `mistral-medium-3-5` (main) + `v4-flash` (fast) | $0.44 | **$13.20** |
| `v4-pro-0813` (main) + `v4-flash` (fast) | $0.60 | **$18.00** |

---

## Qualitative Observations

### DeepSeek V4 Pro (both variants)
- **Strengths:** Structured output (tables, sections), comprehensive coverage, mentions legal disclaimers
- **Weaknesses:** Sometimes overly verbose (0813 variant used 2488 tokens on torrent search vs 487 for flash)
- **Best for:** Complex agent reasoning, multi-step tool orchestration

### DeepSeek V4 Flash (both variants)
- **Strengths:** Incredible cost efficiency, nearly identical quality to Pro, fast enough
- **Weaknesses:** Slightly less structured output, occasionally misses confirmation nuance
- **Best for:** Summaries, quick replies, high-volume agent interactions

### Llama 4 Maverick
- **Strengths:** Very fast on torrent/direct download prompts, good quality
- **Weaknesses:** Inconsistent speed (8.5s on one prompt, 70s on another), no reasoning support
- **Best for:** Alternative when DeepSeek is down

### Mistral Medium 3.5
- **Strengths:** Fastest overall (7.8s avg), highest quality (96%)
- **Weaknesses:** 78x more expensive than DeepSeek Flash
- **Best for:** When speed is critical and cost is no concern

### Grok 4.3
- **Strengths:** Very fast (7.3s avg)
- **Weaknesses:** Lowest quality (76%), missed key criteria on torrent search
- **Best for:** Not recommended for agent use — too unreliable

---

## Recommendations

### Immediate: Update Default Config

```python
# config.py LLMConfig defaults
model: str = "deepseek-v4-pro"        # was empty, now explicit
fast_model: str = "deepseek-v4-flash"  # already the default — keep it
```

### For Users

1. **If you have this OpenRouter key:** Use DeepSeek models — Anthropic/OpenAI/Google are blocked
2. **To unlock all providers:** Add credits at https://openrouter.ai/settings
3. **Best free-tier combo:** `deepseek-v4-pro` (main) + `deepseek-v4-flash` (fast)
4. **If DeepSeek is slow/down:** `meta-llama/llama-4-maverick` is a solid fallback

### For the Model Dropdown

The current OpenRouter preset includes 15 models. Based on this benchmark:
- **Keep all DeepSeek variants** — they're the workhorses
- **Keep Llama 4 Maverick** — good fallback
- **Keep Mistral Medium 3.5** — best for speed-sensitive users
- **Keep Grok 4.3** — usable for fast model if user prefers
- **Anthropic/OpenAI/Google models** — keep in list (users with credits can use them), but add a note that they require OpenRouter credits

---

## Files Created

| File | Purpose |
|---|---|
| `benchmark_models.py` | Reusable benchmark script — `python benchmark_models.py --quick` for fast tests |
| `benchmark_results.json` | Raw benchmark data (all 42 results with scores, costs, content) |
| `SEARCH_STUDY.md` | Comprehensive study of search/download architecture |
| `BENCHMARK_REPORT.md` | This report |
