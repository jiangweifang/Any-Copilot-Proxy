"""Synthetic context retrieval probes; no generation requests without --run."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import random
import secrets
from typing import Protocol

import aiohttp

from ..config import ListenerSettings
from .protocol import ChatInput

SIZES = (16000, 32000, 64000, 128000)
WORDS = ("amber birch cobalt delta elm fern granite harbor iris jade kelp linen maple nickel oak pearl "
         "quartz river silver timber umber violet willow xenon yarrow zinc").split()


class Encoding(Protocol):
    def encode(self, text: str, *, disallowed_special=()) -> list[int]: ...
    def decode(self, tokens: list[int]) -> str: ...


def build_probe(model: str, target: int, encoding: Encoding, seed: int) -> tuple[dict, dict, int]:
    randomizer = random.Random(seed)
    expected = {f"CHECKPOINT_{index}": f"{randomizer.getrandbits(64):016x}" for index in range(5)}
    noise = " ".join(randomizer.choice(WORDS) for _ in range(target))
    tokens = encoding.encode(noise, disallowed_special=())

    def request(length):
        pieces = ["Read the complete document. Five CHECKPOINT records are embedded in it.\n"]
        for index, (key, value) in enumerate(expected.items()):
            if index:
                pieces.append(encoding.decode(tokens[length * (index - 1) // 4:length * index // 4]))
            pieces.append(f"\n{key}={value}\n")
        pieces.append("\nReturn only a JSON object mapping every CHECKPOINT_0 through CHECKPOINT_4 to its exact value.")
        return {"model": model, "messages": [{"role": "user", "content": "".join(pieces)}], "stream": False}

    low, high = 0, len(tokens)
    while low < high:
        middle = (low + high + 1) // 2
        count = len(encoding.encode(ChatInput.parse(request(middle)).prompt, disallowed_special=()))
        if count <= target:
            low = middle
        else:
            high = middle - 1
    payload = request(low)
    count = len(encoding.encode(ChatInput.parse(payload).prompt, disallowed_special=()))
    return payload, expected, count


def retrieved(content: str, expected: dict) -> bool:
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        result = json.loads(text)
    except ValueError:
        return False
    return isinstance(result, dict) and all(result.get(key) == value for key, value in expected.items())


async def execute(args, encoding: Encoding):
    listener = ListenerSettings.load(args.root)
    seed = secrets.randbits(32)
    records = []
    probes = []
    for size in SIZES:
        payload, expected, count = build_probe(args.model, size, encoding, seed + size)
        probes.append((size, payload, expected, count))
    print(json.dumps({"mode": "live" if args.run else "dry-run", "model": args.model, "referenceTokenizer": args.encoding,
                      "officialTokenCounts": False, "maximumGenerationRequests": 6,
                      "note": "Reference tokenizer counts are estimates for this M365 model, not server usage or a certified context limit.",
                      "probes": [{"target": size, "referencePromptTokens": count,
                                  "promptCharacters": len(payload["messages"][0]["content"])}
                                 for size, payload, _, count in probes]}, indent=2))
    if not args.run:
        return
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=330)) as session:
        async with session.get(listener.base_url + "/models") as response:
            if response.status != 200:
                raise RuntimeError(f"Local model discovery failed: HTTP {response.status}.")
            catalog = await response.json()
        if args.model not in {model["id"] for model in catalog["data"]}:
            raise ValueError("The chosen model is absent from the local catalog.")

        async def run_probe(probe, repetition=0):
            size, payload, expected, count = probe
            async with session.post(listener.base_url + "/chat/completions", json=payload) as response:
                result = await response.json(content_type=None)
                accepted = response.status == 200 and isinstance(result.get("choices"), list) and bool(result["choices"])
                content = result["choices"][0]["message"].get("content") or "" if accepted else ""
                record = {"target": size, "referencePromptTokens": count, "httpStatus": response.status,
                          "accepted": accepted, "allCheckpointsRetrieved": accepted and retrieved(content, expected),
                          "repetition": repetition, "serverUsage": result.get("usage")}
                records.append(record)
                print(json.dumps(record))
                if response.status in (401, 403, 429):
                    raise RuntimeError(f"Probe stopped after HTTP {response.status}; no automatic retry.")
                return record["allCheckpointsRetrieved"]

        best = None
        for probe in probes:
            if await run_probe(probe):
                best = probe
            else:
                break
        if best:
            for repetition in (1, 2):
                payload, expected, count = build_probe(args.model, best[0], encoding, seed + best[0] + repetition)
                await run_probe((best[0], payload, expected, count), repetition)
        print(json.dumps({"results": records,
                          "interpretation": "A passing length demonstrates retrieval for these probes only. Failure may be truncation, model retrieval quality or a transport limit; it does not identify a precise model context window."}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--model", default="m365/Gpt_62_Astra_Reasoning@paid")
    parser.add_argument("--encoding", default="o200k_base", help="Reference tokenizer, not a claim about the server tokenizer")
    parser.add_argument("--run", action="store_true", help="Allow up to six real inference requests using synthetic text")
    args = parser.parse_args()
    try:
        import tiktoken
        encoding = tiktoken.get_encoding(args.encoding)
        asyncio.run(execute(args, encoding))
    except ImportError:
        parser.exit(1, 'Install the optional benchmark dependencies: python -m pip install -c constraints.txt -e ".[benchmark]"\n')
    except (ValueError, RuntimeError, aiohttp.ClientError, OSError, TimeoutError) as error:
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "Benchmark network request failed."
        parser.exit(1, message + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
