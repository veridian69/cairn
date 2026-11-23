"""Measure edge-dedup accuracy of the shipped resolve_edge paths on a labelled corpus.

Drives Cairn's own provider client and ``EdgeBatcher`` against the live
provider: the single call Graphiti issues (small model) and the combined call
the batcher issues (medium model). ``--compare-batch-model`` repeats the
combined call on another model, for a before-and-after comparison. No Docker,
graph or catalogue is used. A designated test-provider key is mandatory.

Exit 0 means the measurement completed, whatever it found; 2 means it did not.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

from semantic_memory_environment import EnvironmentError, provider_environment

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "tests/fixtures/memory_quality/edge-dedup-v1.json"
SCHEMA = "cairn.edge-dedup-corpus.v1"


def load_corpus(path: Path) -> tuple[list[dict[str, Any]], str]:
    raw = path.read_bytes()
    document = json.loads(raw)
    if document.get("schema") != SCHEMA or not document.get("items"):
        raise ValueError("corpus_invalid")
    for item in document["items"]:
        existing = len(item["existing"])
        total = existing + len(item["invalidation"])
        contradicted = item["contradicted"]
        if (
            not all(0 <= idx < existing for idx in item["duplicates"])
            or (
                contradicted is not None
                and not all(0 <= idx < total for idx in contradicted)
            )
            or item["new"] in item["existing"]
        ):
            raise ValueError("corpus_invalid")
    return document["items"], hashlib.sha256(raw).hexdigest()


def context(item: dict[str, Any]) -> dict[str, Any]:
    """The context Graphiti's resolve_extracted_edge builds, idx continuous."""
    offset = len(item["existing"])
    return {
        "existing_edges": [
            {"idx": i, "fact": fact} for i, fact in enumerate(item["existing"])
        ],
        "new_edge": item["new"],
        "edge_invalidation_candidates": [
            {"idx": offset + i, "fact": fact}
            for i, fact in enumerate(item["invalidation"])
        ],
    }


def score(item: dict[str, Any], answer: dict[str, Any]) -> dict[str, Any]:
    """Compare one answer with its label.

    Graphiti drops duplicate idx values outside EXISTING FACTS before using
    them, so they are counted here but do not make a false duplicate.
    """
    existing = len(item["existing"])
    returned = set(answer.get("duplicate_facts") or [])
    usable = {idx for idx in returned if 0 <= idx < existing}
    expected = set(item["duplicates"])
    contradicted = item["contradicted"]
    return {
        "id": item["id"],
        "duplicates_correct": usable == expected,
        "false_duplicates": len(usable - expected),
        "missed_duplicates": len(expected - usable),
        "out_of_range": len(returned - usable),
        "contradictions_correct": None
        if contradicted is None
        else set(answer.get("contradicted_facts") or []) == set(contradicted),
        "duplicate_facts": sorted(returned),
        "contradicted_facts": sorted(answer.get("contradicted_facts") or []),
    }


def summarise(runs: list[list[dict[str, Any]]]) -> dict[str, Any]:
    results = [result for run in runs for result in run]
    scored = [r for r in results if r["contradictions_correct"] is not None]
    return {
        "duplicates_correct_per_repeat": [
            sum(r["duplicates_correct"] for r in run) for run in runs
        ],
        "items_per_repeat": len(runs[0]),
        "false_duplicates": sum(r["false_duplicates"] for r in results),
        "missed_duplicates": sum(r["missed_duplicates"] for r in results),
        "out_of_range": sum(r["out_of_range"] for r in results),
        "contradictions_correct": sum(r["contradictions_correct"] for r in scored),
        "contradictions_scored": len(scored),
    }


async def measure(
    items: list[dict[str, Any]], repeats: int, compare_models: list[str]
) -> dict[str, Any]:
    # Imported here: the provider key must be in the environment first.
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_client import OpenAIClient

    from cairn.projection import graphiti as adapter
    from cairn.projection import graphiti_edge_batch as seam
    from cairn.projection.models import LLM_MEDIUM_MODEL, LLM_SMALL_MODEL
    from cairn.runtime.config import GraphitiConfig

    # The shipped defaults, read without building a whole configuration.
    fields = GraphitiConfig.model_fields
    size = int(fields["edge_batch_size"].default)
    linger_ms = int(fields["edge_batch_linger_ms"].default)
    max_facts = int(fields["edge_batch_max_facts"].default)
    shared, shipped, _, _ = adapter._openai_provider_clients()
    clients = {LLM_MEDIUM_MODEL: shipped}
    for model in compare_models:
        clients[model] = OpenAIClient(
            config=LLMConfig(model=model, small_model=LLM_SMALL_MODEL),
            client=shared,
            reasoning="none",
        )

    single_arm = f"single:{LLM_SMALL_MODEL}"
    calls: dict[str, dict[str, int]] = {}

    def counted(client: Any, arm: str) -> Any:
        """The client's call, counting provider calls by prompt name.

        The batcher answers a dropped item with a single call, so only the
        counts show that an arm's answers came from combined calls.
        """
        tally = calls.setdefault(arm, {})

        async def call(messages: Any, **keywords: Any) -> dict[str, Any]:
            name = str(keywords.get("prompt_name"))
            tally[name] = tally.get(name, 0) + 1
            result: dict[str, Any] = await client.generate_response(
                messages, **keywords
            )
            return result

        return call

    async def batched(model: str, order: list[dict[str, Any]]) -> list[dict[str, Any]]:
        batcher = seam.EdgeBatcher(
            counted(clients[model], f"batch:{model}"),
            batch_size=size,
            linger_seconds=linger_ms / 1000,
            max_facts=max_facts,
        )
        answers = await asyncio.gather(
            *(batcher.submit(context(item), None) for item in order)
        )
        return [
            score(item, answer) for item, answer in zip(order, answers, strict=True)
        ]

    arms: dict[str, list[list[dict[str, Any]]]] = {
        single_arm: [],
        **{f"batch:{model}": [] for model in clients},
    }
    try:
        for repeat in range(repeats):
            # Batch composition changes what the model sees beside each item.
            order = list(items)
            random.Random(repeat).shuffle(order)
            singles = await asyncio.gather(
                *(
                    seam._single_resolve(
                        counted(shipped, single_arm), context(item), None
                    )
                    for item in order
                )
            )
            arms[single_arm].append(
                [
                    score(item, answer)
                    for item, answer in zip(order, singles, strict=True)
                ]
            )
            for model in clients:
                arms[f"batch:{model}"].append(await batched(model, order))
    finally:
        await shared.close()
    return {
        "edge_batch": {
            "size": size,
            "linger_ms": linger_ms,
            "max_facts": max_facts,
        },
        "arms": {
            name: {
                "summary": summarise(runs),
                "provider_calls": calls[name],
                "repeats": runs,
            }
            for name, runs in arms.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--compare-batch-model", action="append", default=[])
    args = parser.parse_args()
    try:
        items, digest = load_corpus(args.corpus)
        environment = provider_environment(args.provider_key_file, os.environ)
    except (OSError, ValueError, KeyError, EnvironmentError) as error:
        print(f"edge_dedup_evaluation_refused: {error}", file=sys.stderr)
        return 2
    # Only the helper's whitelist: an ambient OPENAI_BASE_URL, proxy or
    # organisation setting would otherwise redirect the key and the corpus.
    os.environ.clear()
    os.environ.update(environment)
    # Graphiti and the SDK log provider error text before re-raising, and that
    # text can quote request content. Nothing here needs their logs.
    logging.disable(logging.CRITICAL)
    try:
        measured = asyncio.run(measure(items, args.repeats, args.compare_batch_model))
    except Exception as error:
        # The type only: provider errors can quote request content.
        print(f"edge_dedup_evaluation_failed: {type(error).__name__}", file=sys.stderr)
        return 2
    report = {
        "schema": "cairn.edge-dedup-evaluation.v1",
        "corpus_sha256": digest,
        "graphiti_core": version("graphiti-core"),
        "openai": version("openai"),
        "repeats": args.repeats,
        **measured,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    for name, arm in report["arms"].items():
        print(name, json.dumps(arm["summary"]), json.dumps(arm["provider_calls"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
