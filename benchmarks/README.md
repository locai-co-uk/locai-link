<!-- SPDX-FileCopyrightText: 2026 Loc.ai Ltd. -->
<!-- SPDX-License-Identifier: BUSL-1.1 -->

# Benchmarks

Local performance benchmarks for the Link agent's hot paths. These are **not**
part of the normal test suite (`testpaths` is `tests/`), so `uv run pytest`
never picks them up — run them explicitly.

They exist to answer one question: *did a change help or hurt?* Capture a
baseline, make your change, compare.

## What's measured

| File | Path exercised |
|------|----------------|
| `bench_command_parse.py` | Per-command template resolve + schema parse |
| `bench_telemetry.py` | Log/telemetry emit: shape + JSON encode + enqueue |
| `bench_swap_config.py` | llama-swap multi-model config generation |
| `bench_proxy.py` | Proxy GET round-trip **and** upstream connection reuse (2.2) |
| `bench_pipeline_idle.py` | Idle pipeline wakeup rate (2.5) |
| `bench_runtime_contention.py` | Control-plane read responsiveness + lock-split deadlock guard (2.1) |

`bench_proxy.py`, `bench_pipeline_idle.py`, and `bench_runtime_contention.py`
also carry plain assertions that double as regression guards (connection reuse;
bounded idle wakeups; responsive reads + no deadlock under a slow start).

## Run

```bash
# Run everything
uv run pytest benchmarks/ -v

# Timing benchmarks only (skip the -s output from the guard tests)
uv run pytest benchmarks/ --benchmark-only
```

## Before/after comparison

```bash
# 1. Save a baseline (writes to .benchmarks/)
uv run pytest benchmarks/ --benchmark-only --benchmark-autosave
# -> Saved to .benchmarks/.../0001_....json

# 2. Make your change, then compare against the saved run
uv run pytest benchmarks/ --benchmark-only --benchmark-compare=0001

# Fail locally if the mean regresses >10%
uv run pytest benchmarks/ --benchmark-only --benchmark-compare=0001 \
  --benchmark-compare-fail=mean:10%
```

`.benchmarks/` is git-ignored territory and is already on `reset`'s cleanup
list; baselines are local, not committed.

## Deep profiling with py-spy

When a benchmark flags a regression, find *why* against a running agent — no
code change, all threads sampled:

```bash
# Flamegraph of where wall-time goes across every thread
uv run py-spy record -o flame.svg --pid <agent-pid>

# Instant snapshot of what each thread is doing right now
# (fastest way to see what's holding a lock / why the agent is stalled)
uv run py-spy dump --pid <agent-pid>

# Live top-style view
uv run py-spy top --pid <agent-pid>
```

Inference runs in `llama-server` subprocesses, so py-spy on the agent shows
orchestration cost only. For end-to-end serving latency, measure
time-to-first-token at the proxy.
