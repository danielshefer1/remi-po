"""Stage 1 of #78: compare hosted embedding models on the 20-question Qwen test set.

`--check` runs offline. Live mode needs VOYAGE_API_KEY and COHERE_API_KEY in the environment.
"""

import json
import math
import os
import pathlib
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import httpx

SET_PATH = pathlib.Path(__file__).with_name("embedding_bakeoff_set.json")
DIMENSION = 1024
CONDITIONS = ("english", "hebrew", "mixed")


@dataclass(frozen=True)
class Candidate:
    model: str
    vendor: Literal["voyage", "cohere"]
    sends_dimension: bool


CANDIDATES = (
    Candidate("voyage-4-large", "voyage", True),
    Candidate("voyage-4", "voyage", True),
    Candidate("embed-v5.0-pro", "cohere", True),
    Candidate("embed-multilingual-v3.0", "cohere", False),
)


def is_hebrew(text: str) -> bool:
    return any("֐" <= ch <= "׿" for ch in text)


def rank(question: Sequence[float], notes: Sequence[Sequence[float]]) -> list[int]:
    def cosine(note: Sequence[float]) -> float:
        dot = sum(a * b for a, b in zip(question, note, strict=True))
        return dot / (math.hypot(*question) * math.hypot(*note))

    return sorted(range(len(notes)), key=lambda i: cosine(notes[i]), reverse=True)


def embed(
    client: httpx.Client,
    candidate: Candidate,
    texts: Sequence[str],
    kind: Literal["document", "query"],
) -> tuple[list[list[float]], int]:
    voyage = candidate.vendor == "voyage"
    if voyage:
        url, key = "https://api.voyageai.com/v1/embeddings", os.environ["VOYAGE_API_KEY"]
        body: dict[str, object] = {
            "model": candidate.model,
            "input": list(texts),
            "input_type": kind,
        }
    else:
        url, key = "https://api.cohere.com/v2/embed", os.environ["COHERE_API_KEY"]
        body = {
            "model": candidate.model,
            "texts": list(texts),
            "input_type": f"search_{kind}",
            "embedding_types": ["float"],
        }
    if candidate.sends_dimension:
        body["output_dimension"] = DIMENSION
    for _ in range(6):  # the first try plus at most 5 retries
        response = client.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        if response.status_code != 429:
            break
        time.sleep(25)
    if not response.is_success:
        raise SystemExit(f"{candidate.model}: HTTP {response.status_code}")
    payload = response.json()
    if voyage:
        rows = sorted(payload["data"], key=lambda row: row["index"])
        return [row["embedding"] for row in rows], payload["usage"]["total_tokens"]
    return payload["embeddings"]["float"], int(payload["meta"]["billed_units"]["input_tokens"])


def self_check(data: dict[str, dict[str, list]]) -> None:
    for condition in CONDITIONS:
        assert len(data[condition]["notes"]) == 20, condition
        assert len(data[condition]["questions"]) == 20, condition
    mixed = data["mixed"]
    crossed = sum(is_hebrew(q) != is_hebrew(mixed["notes"][i]) for q, i in mixed["questions"])
    assert crossed == 7, crossed
    assert rank([1.0, 0.0], [[0.0, 1.0], [1.0, 0.0], [0.9, 0.1]]) == [1, 2, 0]


def main() -> None:
    data = json.loads(SET_PATH.read_text(encoding="utf-8"))
    self_check(data)
    if "--check" in sys.argv:
        sys.stdout.write("self-check ok: 3 conditions, 7 cross-language questions\n")
        return
    lines = [
        "| model | tokens | english | hebrew | mixed | mixed same-lang | mixed Hebrew-English |",
        "|---|---|---|---|---|---|---|",
    ]
    misses: list[str] = []
    with httpx.Client(timeout=60) as client:
        for cand in CANDIDATES:
            tokens = 0
            tops: dict[str, list[int]] = {name: [] for name in (*CONDITIONS, "same", "cross")}
            for condition in CONDITIONS:
                notes, questions = data[condition]["notes"], data[condition]["questions"]
                note_vecs, used_n = embed(client, cand, notes, "document")
                question_vecs, used_q = embed(client, cand, [q for q, _ in questions], "query")
                tokens += used_n + used_q
                for n, ((question, want), vec) in enumerate(
                    zip(questions, question_vecs, strict=True)
                ):
                    order = rank(vec, note_vecs)
                    place = order.index(want)  # 0 is a hit@1, under 3 a hit@3
                    tops[condition].append(place)
                    if condition == "mixed":
                        crossed = is_hebrew(question) != is_hebrew(notes[want])
                        tops["cross" if crossed else "same"].append(place)
                    if place:
                        misses.append(
                            f"{cand.model} {condition} q{n}: wanted note {want}, top note {order[0]}"  # noqa: E501
                        )
            cells = [
                f"{sum(p < 3 for p in v)}/{len(v)} ({sum(p == 0 for p in v)})"
                for v in tops.values()
            ]
            lines.append("| " + " | ".join([cand.model, str(tokens), *cells]) + " |")
    sys.stdout.write("\n".join(lines) + "\n\nMisses at rank 1:\n" + "\n".join(misses) + "\n")


if __name__ == "__main__":
    main()
