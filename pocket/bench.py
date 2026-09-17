"""The benchmark, embedded rather than rewritten.

This is tiiny-bench (/Users/sem/code/tiiny/tiiny-bench) with two changes and no
new measurements invented:

  1. Every request goes through the device's Pocket lock, so the benchmark takes
     its turn alongside the Chat page instead of colliding with it.
  2. It runs against a device chosen from the fleet rather than a hardcoded
     address, and results are saved under Pocket's data directory.

The four tests are the original ones, and they measure what a spec sheet does
not: how prefill scales with prompt length, whether throughput holds over a long
generation, what happens when several callers arrive at once, and what a
reasoning model charges in wall time for tokens nobody reads.

Nothing is loaded, unloaded or deleted. The benchmark runs against whatever is
already loaded on the chosen device, which is what makes it safe on a box doing
real work.

One honest note about the concurrency test. On real hardware the NPU does not
batch, so aggregate throughput is flat and a second caller arriving mid-request
gets error 150004. Through Pocket's lock that same load turns into a queue: the
aggregate number stays flat for the same reason, but nothing fails. The test
therefore measures queueing behaviour, which is what a caller of this endpoint
actually experiences.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time

from . import device as device_mod
from .fleet import data_dir

FILLER = ("The quick brown fox jumps over the lazy dog near the riverbank at dawn. "
          "Engineers measured the throughput carefully and recorded every result. ")


def results_dir():
    target = os.path.join(data_dir(), "bench")
    os.makedirs(target, mode=0o700, exist_ok=True)
    return target


class Run:
    """One benchmark run, with a log the web UI can poll while it works."""

    def __init__(self, device_id, label, only, model=None):
        self.device_id = device_id
        self.label = label
        self.only = only
        # The model somebody chose, or None to take the first loaded one that
        # can chat.
        self.model = model
        self.started = time.time()
        self.finished = None
        self.state = "running"
        self.error = None
        self.record = None
        self.lines = []
        self._guard = threading.Lock()

    def log(self, text):
        with self._guard:
            self.lines.append(text)

    def snapshot(self):
        with self._guard:
            return {"device": self.device_id, "label": self.label,
                    "model": self.model,
                    "state": self.state, "error": self.error,
                    "started": self.started, "finished": self.finished,
                    "elapsed_s": round((self.finished or time.time()) - self.started, 1),
                    "lines": list(self.lines),
                    "saved_as": (self.record or {}).get("saved_as")}


def _telemetry(dev):
    """NPU load context alongside every number. Tokens per second without the
    load context is not evidence."""
    try:
        status = dev.sys_status()
    except device_mod.DeviceError:
        return {}
    npus = (status.get("npus") or [{}])[0] if isinstance(status.get("npus"), list) else {}
    cpu = status.get("cpu") or {}
    return {"npu_util_pct": npus.get("utilization_percent"),
            "npu_mem_used_mb": npus.get("memory_used_mb"),
            "npu_mem_total_mb": npus.get("memory_total_mb"),
            "cpu_total_pct": cpu.get("total_percent")}


def derive_stats(timings, usage, wall_s=None):
    """The numbers a completion reports about itself, from the gateway's own blocks.

    This is the benchmark's measurement and the Chat page shows the same numbers
    under the same names, so there is one derivation and both callers use it. Two
    of these would drift the first time the gateway renamed a field, and then the
    chat bar and the saved benchmark would disagree about the same request.

    `timings` and `usage` are the blocks the gateway sends: at the top level of a
    non-streamed completion, and in the final chunk of a stream asked for with
    stream_options.include_usage. Nothing here is computed from a clock on this
    side, which is why the chat route adds its own measured ttft_ms on top rather
    than replacing ttft_s: ttft_s is prefill plus one token's decode time, the
    only answer available when the whole reply arrives at once.
    """
    timings = timings or {}
    usage = usage or {}
    stats = {
        "prompt_tokens": usage.get("prompt_tokens", timings.get("prompt_n", 0)),
        "out_tokens": usage.get("completion_tokens", timings.get("predicted_n", 0)),
        "prefill_tok_s": round(timings.get("prompt_per_second") or 0, 2),
        "decode_tok_s": round(timings.get("predicted_per_second") or 0, 2),
        "prefill_ms": round(timings.get("prompt_ms") or 0, 1),
        "ttft_s": round((timings.get("prompt_ms") or 0) / 1000
                        + (timings.get("predicted_per_token_ms") or 0) / 1000, 3),
        "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
    }
    if wall_s is not None:
        stats["wall_s"] = round(wall_s, 3)
    return stats


def _chat(dev, lock, model, prompt, max_tokens, thinking=False, timeout=240):
    body = {"model": model, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": thinking},
            "messages": [{"role": "user", "content": prompt}]}
    started = time.time()
    try:
        with lock.turn(why="bench"):
            payload = dev.chat(body, timeout=timeout)
    except device_mod.DeviceError as exc:
        return {"error": str(exc)[:160], "wall_s": round(time.time() - started, 2)}
    wall = time.time() - started
    return derive_stats((payload or {}).get("timings"), (payload or {}).get("usage"), wall)


def t_prefill(run, dev, lock, model, reps=(2, 12, 60, 240)):
    """How fast does it ingest a document? Prefill is what long context costs."""
    run.log("PREFILL SCALING (document ingestion)")
    rows = []
    for count in reps:
        prompt = (FILLER * count) + "\n\nReply with the single word: ok"
        got = _chat(dev, lock, model, prompt, 4)
        if "error" in got:
            run.log("  %d reps failed: %s" % (count, got["error"][:60]))
            continue
        rows.append(got)
        run.log("  %6d prompt tokens   %8.2f tok/s prefill   %8.1f ms"
                % (got["prompt_tokens"], got["prefill_tok_s"], got["prefill_ms"]))
    return rows


def t_sustained(run, dev, lock, model, total=1500):
    """Does throughput hold, or sag as the box heats and the KV cache grows?"""
    run.log("SUSTAINED GENERATION (%d tokens, one unbroken request)" % total)
    before = _telemetry(dev)
    got = _chat(dev, lock, model,
                "Write a detailed technical explanation of how speculative decoding "
                "works in large language model inference. Cover the draft model, "
                "verification, acceptance rates, and why throughput varies with "
                "content.", total)
    after = _telemetry(dev)
    if "error" in got:
        run.log("  failed: %s" % got["error"][:80])
        return None
    run.log("  %d tokens in %ss at %s tok/s"
            % (got["out_tokens"], got["wall_s"], got["decode_tok_s"]))
    run.log("  NPU mem %s/%s MB" % (after.get("npu_mem_used_mb"),
                                    after.get("npu_mem_total_mb")))
    return {"run": got, "telemetry_before": before, "telemetry_after": after}


def t_concurrency(run, dev, lock, model, levels=(1, 2, 4, 8), per=160):
    """Aggregate throughput as more callers arrive at the same time.

    Through the lock these queue. On the raw device they would collide.
    """
    run.log("CONCURRENCY (%d tokens per request, queued through the device lock)" % per)
    rows = []
    for parallel in levels:
        done, failed = [], []

        def worker(index):
            got = _chat(dev, lock, model,
                        "Explain concept number %d: why memory bandwidth limits "
                        "token generation on edge devices. Be specific." % index, per)
            (failed if "error" in got else done).append(got)

        started = time.time()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(parallel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        wall = time.time() - started
        if not done:
            run.log("  %2d parallel: all failed" % parallel)
            continue
        aggregate = sum(row["out_tokens"] for row in done) / wall
        per_stream = statistics.median(row["decode_tok_s"] for row in done)
        rows.append({"parallel": parallel, "aggregate_tok_s": round(aggregate, 2),
                     "per_stream_tok_s": round(per_stream, 2),
                     "wall_s": round(wall, 2), "ok": len(done), "failed": len(failed)})
        run.log("  %2d parallel: %8.2f tok/s aggregate   %6.2f per stream   %6.2fs wall"
                % (parallel, aggregate, per_stream, wall))
    return rows


def t_thinking(run, dev, lock, model):
    """A reasoning model splits output. What do the hidden tokens cost?"""
    run.log("REASONING COST (same prompt, thinking on vs off)")
    question = ("A train leaves at 3pm going 60mph. Another leaves at 4pm going "
                "80mph. When does the second catch the first?")
    out = {}
    for name, flag in (("off", False), ("on", True)):
        got = _chat(dev, lock, model, question, 700, thinking=flag)
        if "error" in got:
            run.log("  thinking %-3s failed: %s" % (name, got["error"][:60]))
            continue
        out[name] = got
        run.log("  thinking %-3s %4d tokens  %6.2fs  %6.2f tok/s"
                % (name, got["out_tokens"], got["wall_s"], got["decode_tok_s"]))
    if "on" in out and "off" in out and out["off"]["wall_s"]:
        ratio = out["on"]["wall_s"] / out["off"]["wall_s"]
        run.log("  reasoning costs %.1fx the wall time" % ratio)
    return out


TESTS = {"prefill": t_prefill, "sustained": t_sustained,
         "concurrency": t_concurrency, "thinking": t_thinking}


def _chat_capable(dev, running):
    """Filter a device's running list down to the models that can chat.

    Keeps the device's own order, so a box with one chat model loaded gets that
    one and a box with several still gets the first. The model list is asked
    for once; if the device will not answer it, nothing is filtered out rather
    than the benchmark refusing to run at all.
    """
    try:
        rows = dev.models()
    except device_mod.DeviceError:
        return list(running)
    kinds = {}
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("model_id") or entry.get("id") or entry.get("name")
        if model_id:
            kinds[model_id] = device_mod.can_chat(
                entry.get("type"), device_mod.capability_list(entry))
    return [m for m in running if kinds.get(m, True)]


def execute(run, fleet):
    """Run the suite. Called on a worker thread by start()."""
    try:
        dev = fleet.get(run.device_id)
        lock = fleet.locks[run.device_id]
        running = list((dev.running().get("running") or []))
        # Every test in this suite is a chat completion, so the model has to be
        # one that answers a chat completion. A box commonly has an embedding or
        # a speech model loaded first, and taking whatever is at the front of
        # the running list benchmarks that and reports numbers for a model that
        # never ran a token.
        chatty = _chat_capable(dev, running)
        if run.model:
            model = run.model
            if model not in running:
                raise RuntimeError(
                    "%s is not loaded on %s. The benchmark runs against what is "
                    "already loaded and never loads anything itself; loaded "
                    "there: %s." % (model, dev.name,
                                    ", ".join(running) if running else "nothing"))
            if model not in chatty:
                raise RuntimeError(
                    "%s cannot chat, and every section of this suite is a chat "
                    "completion. Loaded on %s and able to chat: %s."
                    % (model, dev.name,
                       ", ".join(chatty) if chatty else "nothing"))
        else:
            model = next((m for m in chatty), None)
        if not model:
            if running:
                raise RuntimeError(
                    "nothing loaded on %s can chat: %s. The benchmark measures "
                    "chat completions, so load a text generation model on the "
                    "Models page first; it never loads or unloads anything "
                    "itself." % (dev.name, ", ".join(running)))
            raise RuntimeError("no model loaded on %s. Load one on the Models page "
                               "first; the benchmark never loads or unloads "
                               "anything itself." % dev.name)
        run.model = model
        info = dev.device_info()
        units = dev.npu_units()
        telemetry = _telemetry(dev)
        run.log("device : %s (%s)" % (dev.name, dev.address or dev.gateway))
        run.log("model  : %s" % model)
        run.log("build  : TiinyOS %s   NPU %s/%s units"
                % (info.get("tiiny_os"), units.get("npu_used"), units.get("npu_total")))
        run.log("")
        wanted = run.only or list(TESTS)
        results = {}
        for name in wanted:
            test = TESTS.get(name)
            if test is None:
                run.log("unknown test: %s" % name)
                continue
            results[name] = test(run, dev, lock, model)
            run.log("")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        record = {"label": run.label, "stamp": stamp, "device": dev.id,
                  "device_name": dev.name, "model": model,
                  "build": info.get("tiiny_os"),
                  "npu_units": units.get("npu_used"),
                  "npu_total": units.get("npu_total"),
                  "npu_mem_total_mb": telemetry.get("npu_mem_total_mb"),
                  "tests": wanted, "results": results,
                  "notes": ["requests were serialised through the AINode Pocket "
                            "device lock, so the concurrency rows show queueing "
                            "rather than collisions"]}
        name = "%s-%s.json" % (stamp, _slug(run.label))
        path = os.path.join(results_dir(), name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
        record["saved_as"] = name
        run.record = record
        run.state = "done"
        run.log("saved %s" % name)
    except Exception as exc:  # a benchmark must not take the server down
        run.state = "error"
        run.error = str(exc)
        run.log("error: %s" % exc)
    finally:
        run.finished = time.time()


def _slug(text):
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in (text or "run"))
    return safe.strip("-")[:40] or "run"


def start(fleet, device_id, label, only=None, model=None):
    run = Run(device_id, label, only, model)
    threading.Thread(target=execute, args=(run, fleet), daemon=True).start()
    return run


def history():
    """Saved results, newest first, with just enough to render a list."""
    out = []
    for name in sorted(os.listdir(results_dir()), reverse=True):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(results_dir(), name), "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except Exception:
            continue
        summary = {"saved_as": name, "label": record.get("label"),
                   "stamp": record.get("stamp"), "model": record.get("model"),
                   "device_name": record.get("device_name"),
                   "npu_units": record.get("npu_units"),
                   "npu_total": record.get("npu_total"),
                   "tests": record.get("tests") or list((record.get("results") or {}))}
        results = record.get("results") or {}
        sustained = (results.get("sustained") or {}) or {}
        if isinstance(sustained, dict):
            summary["decode_tok_s"] = ((sustained.get("run") or {}).get("decode_tok_s"))
        rows = results.get("concurrency") or []
        if isinstance(rows, list) and rows:
            summary["aggregate_tok_s"] = rows[-1].get("aggregate_tok_s")
            summary["parallel"] = rows[-1].get("parallel")
        out.append(summary)
    return out


def load(name):
    if os.path.basename(name) != name or not name.endswith(".json"):
        raise ValueError("bad result name")
    with open(os.path.join(results_dir(), name), "r", encoding="utf-8") as handle:
        return json.load(handle)
