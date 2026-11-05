#Eto voobshe kto-ot smotrit?
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TOKEN_FILE = ROOT / "api-token.txt"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results.csv"

SHORT_PROMPT = (
    "In one concise sentence, explain why Kubernetes readiness probes are useful."
)

LONG_CONTEXT = """
Kubernetes manages containerized workloads using declarative resources. A
Deployment describes the desired number of application replicas and the
container image and configuration they should run. The Kubernetes control
plane continuously compares the observed state with the desired state and
takes action when they differ. A Pod is the smallest deployable unit and may
contain one or more closely related containers. A Service provides a stable
network endpoint for reaching a changing set of Pods. A PersistentVolumeClaim
requests storage independently of the lifecycle of an individual Pod, which
is useful for model weights and other data that must survive restarts.
Readiness probes tell Kubernetes whether a container is ready to receive
traffic. Liveness probes help detect a process that is running but unable
to make progress. Resource requests influence scheduling, while resource
limits constrain CPU and memory consumption. ConfigMaps hold non-secret
configuration, and Secrets are intended for sensitive values such as tokens.
An init container can prepare files or data before the main application
container starts. These building blocks make it possible to deploy and
operate services consistently across different environments.
""".strip()

LONG_PROMPT = (
    "Read the following background notes. Then answer this question in one "
    "concise sentence: why should model weights be stored on a PersistentVolumeClaim?\n\n"
    + "\n\n".join([LONG_CONTEXT] * 3)
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure TTFT, latency and throughput of a vLLM Chat Completions API."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="vLLM API base URL (default: http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=DEFAULT_TOKEN_FILE,
        help=f"File containing the API bearer token (default: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--model",
        default="Qwen/Qwen2.5-0.5B-Instruct",
        help="Served model name (default: Qwen/Qwen2.5-0.5B-Instruct)",
    )
    parser.add_argument("--repeats", type=int, default=3, help="Measured requests per workload (default: 3)")
    parser.add_argument("--warmup", type=int, default=1, help="Warm-up requests per workload (default: 1)")
    parser.add_argument("--max-tokens", type=int, default=64, help="Maximum generated tokens (default: 64)")
    parser.add_argument("--concurrency", type=int, default=1, help="Concurrent requests per workload (default: 1)")
    parser.add_argument("--timeout", type=float, default=300.0, help="HTTP timeout in seconds (default: 300)")
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"CSV output path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    if args.repeats < 1:
        parser.error("--repeats must be >= 1")
    if args.warmup < 0:
        parser.error("--warmup must be >= 0")
    if args.max_tokens < 1:
        parser.error("--max-tokens must be >= 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    return args


def read_token(token_file: Path) -> str:
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Cannot read token file: {token_file}") from exc
    if not token:
        raise RuntimeError(f"Token file is empty: {token_file}")
    return token


def perform_request(
    *,
    base_url: str,
    token: str,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
) -> dict[str, float | int]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url=f"{base_url.rstrip('/')}/v1/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/event-stream",
        },
        method="POST",
    )

    started = time.perf_counter()
    first_text_at: float | None = None
    usage: dict | None = None

    try:
        with urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue

                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                if not data:
                    continue

                event = json.loads(data)

                choices = event.get("choices") or []
                for choice in choices:
                    delta = choice.get("delta") or {}
                    text_delta = delta.get("content")
                    if isinstance(text_delta, str) and text_delta and first_text_at is None:
                        first_text_at = time.perf_counter()

                event_usage = event.get("usage")
                if isinstance(event_usage, dict):
                    usage = event_usage

    except HTTPError as exc:
        raise RuntimeError(f"API returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Cannot connect to API: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("API request timed out") from exc

    finished = time.perf_counter()
    latency_s = finished - started

    if first_text_at is None:
        raise RuntimeError("The stream completed without any generated text")
    if not usage:
        raise RuntimeError(
            "The stream did not contain token usage. Check vLLM stream_options.include_usage support."
        )

    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if not isinstance(prompt_tokens, int) or not isinstance(completion_tokens, int):
        raise RuntimeError("The final usage event did not contain valid token counts")

    ttft_s = first_text_at - started
    generation_s = max(latency_s - ttft_s, 0.0)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "ttft_s": ttft_s,
        "latency_s": latency_s,
        "generation_s": generation_s,
        "generation_tokens_per_s": (
            completion_tokens / generation_s if generation_s > 0 else 0.0
        ),
        "end_to_end_tokens_per_s": (
            completion_tokens / latency_s if latency_s > 0 else 0.0
        ),
    }


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile; p95 of a small sample may equal its maximum."""
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def run_workload(
    *,
    name: str,
    prompt: str,
    args: argparse.Namespace,
    token: str,
) -> tuple[list[dict], dict]:
    print(f"\n[{name}] warm-up: {args.warmup}, measured requests: {args.repeats}, "
          f"concurrency: {args.concurrency}")

    for warmup_index in range(args.warmup):
        perform_request(
            base_url=args.base_url,
            token=token,
            model=args.model,
            prompt=prompt,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
        )
        print(f"  warm-up {warmup_index + 1}/{args.warmup}: complete")

    batch_started = time.perf_counter()
    results: list[dict] = []

    if args.concurrency == 1:
        for index in range(args.repeats):
            result = perform_request(
                base_url=args.base_url,
                token=token,
                model=args.model,
                prompt=prompt,
                max_tokens=args.max_tokens,
                timeout=args.timeout,
            )
            result.update({"workload": name, "sample": index + 1})
            results.append(result)
            print(
                f"  request {index + 1}/{args.repeats}: "
                f"TTFT={result['ttft_s']:.3f}s, latency={result['latency_s']:.3f}s"
            )
    else:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [
                executor.submit(
                    perform_request,
                    base_url=args.base_url,
                    token=token,
                    model=args.model,
                    prompt=prompt,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                )
                for _ in range(args.repeats)
            ]
            for index, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                result.update({"workload": name, "sample": index})
                results.append(result)
                print(
                    f"  completed {index}/{args.repeats}: "
                    f"TTFT={result['ttft_s']:.3f}s, latency={result['latency_s']:.3f}s"
                )

    batch_wall_s = time.perf_counter() - batch_started
    total_completion_tokens = sum(int(row["completion_tokens"]) for row in results)
    summary = {
        "workload": name,
        "requests": len(results),
        "concurrency": args.concurrency,
        "batch_wall_s": batch_wall_s,
        "requests_per_s": len(results) / batch_wall_s if batch_wall_s else 0.0,
        "aggregate_output_tokens_per_s": (
            total_completion_tokens / batch_wall_s if batch_wall_s else 0.0
        ),
        "ttft_p50_s": statistics.median(float(row["ttft_s"]) for row in results),
        "ttft_p95_s": percentile([float(row["ttft_s"]) for row in results], 0.95),
        "latency_p50_s": statistics.median(float(row["latency_s"]) for row in results),
        "latency_p95_s": percentile([float(row["latency_s"]) for row in results], 0.95),
    }

    for row in results:
        row["concurrency"] = args.concurrency
        row["batch_wall_s"] = batch_wall_s
        row["batch_requests_per_s"] = summary["requests_per_s"]
        row["batch_output_tokens_per_s"] = summary["aggregate_output_tokens_per_s"]

    return results, summary


def write_csv(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "workload",
        "sample",
        "concurrency",
        "prompt_tokens",
        "completion_tokens",
        "ttft_s",
        "latency_s",
        "generation_s",
        "generation_tokens_per_s",
        "end_to_end_tokens_per_s",
        "batch_wall_s",
        "batch_requests_per_s",
        "batch_output_tokens_per_s",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()
    try:
        token = read_token(args.token_file)
        all_rows: list[dict] = []
        summaries: list[dict] = []

        for name, prompt in (("short", SHORT_PROMPT), ("long", LONG_PROMPT)):
            rows, summary = run_workload(
                name=name,
                prompt=prompt,
                args=args,
                token=token,
            )
            all_rows.extend(rows)
            summaries.append(summary)

        write_csv(all_rows, args.output)

        print("\nSUMMARY")
        print(
            "workload | concurrency | TTFT p50/p95 (s) | "
            "latency p50/p95 (s) | aggregate output tok/s | req/s"
        )
        for item in summaries:
            print(
                f"{item['workload']:8} | {item['concurrency']:11d} | "
                f"{item['ttft_p50_s']:.3f}/{item['ttft_p95_s']:.3f} | "
                f"{item['latency_p50_s']:.3f}/{item['latency_p95_s']:.3f} | "
                f"{item['aggregate_output_tokens_per_s']:.3f} | "
                f"{item['requests_per_s']:.3f}"
            )

        print(f"\nCSV written to: {args.output.resolve()}")
        print("Prompts and generated text were not printed or written to the CSV.")
        return 0
    except (RuntimeError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BENCHMARK ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
