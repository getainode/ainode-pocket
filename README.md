# AINode Pocket

Registers every Tiiny AI Pocket Lab you own, shows what each one is doing, manages the models on them, and puts a single OpenAI-compatible endpoint in front of the whole fleet. For anyone with one or more Tiiny boxes who wants to point ordinary OpenAI clients at them and stop hand-managing which model is loaded where.

```
python3 ainode-pocket --serve
```

One command, one page on port 8430, one base URL for your clients. Python 3.9 or newer, standard library only, no pip install, no accounts, no cloud.

![Overview](docs/screenshots/overview.png)

---

## What changed in 0.1.3

The Chat page reports what every answer cost. This is a benchmarking utility and
the chat was the one place in it that produced no numbers at all, which made it
the only page you could not learn anything from.

Every assistant message now carries a stats bar: time to first token, decode
rate, total wall time, tokens in and out, which device and model served it, and
why generation stopped. The numbers are the device's own `timings` and `usage`
blocks read through `pocket/bench.py:derive_stats`, the same function the
benchmark uses, so a number in the chat bar and the same number in a saved
benchmark mean the same thing. Only two figures are measured on this side: when
the first token arrived, and how long the whole request took. The bar is kept
with the message, so scrolling back up shows what that answer cost rather than
what the last one did.

Around it: a model card for whatever is in the picker, an instances rail showing
every loaded model on every device with an unload button, and a panel for loading
a chat model onto a device. The reasoning block is collapsible and has a toggle
that decides whether reasoning is asked for at all, because on this hardware a
chain of thought spends the same `max_tokens` budget the answer needs, and with a
modest cap it spends all of it.

Four routes are new, all of them the page's own rather than the OpenAI endpoint's:

```
POST /api/chat                 a completion with the numbers attached. Streamed,
                               the last event before [DONE] is `event: stats`
GET  /api/model_card?model=    one model: size, units, capabilities, which
                               devices have it loaded, whether it can chat
GET  /api/instances            every loaded model on every device, live, plus
                               each device's NPU budget
POST /api/instances/load       load a chat model onto one device, 202 and poll
POST /api/instances/unload     unload one, 202
```

`/api/instances/load` refuses a model that is not installed on that device, one
that cannot chat, and one that does not fit the remaining NPU budget. That last
refusal is the interesting one: **the device does not refuse it itself.** Measured
on real hardware, a start that does not fit comes back with the same 200 and the
same `start loading` message a load that fits gets, the model appears in
`npu/status` as `loading`, and then it disappears. No error is returned anywhere.
Worse, the subtraction is not the whole constraint: with a 28 unit model resident
and 64 units free, three separate 50 to 55 unit models were all rolled back. So
the panel refuses the obvious case up front and polls afterwards for the rest,
and a load is only believed when the status says running **and** a one token chat
comes back.

---

## What it does

**Finds and registers devices.** Press Find devices and Pocket broadcasts the
token the device advertises on udp/39217, then checks any USB link this machine
is plugged into. A box answers on Wi-Fi and USB at the same time and reports
both addresses itself, so it registers once with both and prefers the USB link,
which is a fixed point-to-point /30 rather than a DHCP lease that will move. A
subnet scan is there too, opt-in, for a network that drops broadcast.

**Shows what each device is doing.** NPU units used of the 100 available, NPU
memory, storage, CPU, every loaded model with its instance port and unit cost,
and whether the device is busy or has callers queued.

**Manages models per device.** The installed list and the downloadable catalog,
with load, unload, delete, and download with live progress over the device's own
server-sent-events endpoint.

**Serves one endpoint.** `/v1/models` is the union across your devices.
`/v1/chat/completions` routes by model id, streaming or not, over whichever
address and transport that device actually answers on.

**Chats and benchmarks.** A chat page that talks to that same endpoint and
reports what every answer cost, beside a model card and a panel for loading and
unloading models, and the tiiny-bench suite embedded as a page and a CLI
subcommand.

## What it deliberately does not do

- **No training and no fine-tuning.** That is AINode's job, on hardware built for
  it. This is a fleet manager.
- **No quantization or model conversion.** The NPU runs vendor-compiled models
  and its import path accepts exactly one architecture. Nothing here pretends
  otherwise.
- **No container control on the device.** Compose services are listed nowhere in
  this app and never started, stopped, or created. That API is root-equivalent
  and reachable from the LAN; it is not something a fleet dashboard should be
  clicking.
- **No accounts, no TLS, no vendor cloud.** It runs on your machine, beside your
  devices, and talks to them over the LAN exactly as any other client would.
- **No pip dependencies and no GUI framework.** One HTTP server, three files of
  front end, the standard library.

---

## The endpoint contract

Point any OpenAI client at `http://127.0.0.1:8430/v1`. The API key is ignored:
the app already holds your device keys and it does not authenticate its own
callers, which is why it is worth thinking about before binding it to an
address the whole office can reach.

### `GET /v1/models`

The union of every model installed on every registered device. Each row carries
a namespaced block a strict OpenAI client will ignore:

```json
{
  "id": "deepreinforce-ai/Ornith-1.0-35B",
  "object": "model",
  "owned_by": "2 devices",
  "ainode_pocket": {
    "ready": true,
    "chat": true,
    "type": "Image-Text-to-Text",
    "capabilities": ["main"],
    "params": "35B",
    "devices": ["TNY...01Q", "TNY...03Q"],
    "loaded_on": ["TNY...01Q"]
  }
}
```

`ready` means at least one device has it loaded and a request will be served now.
`GET /v1/models?loaded=1` returns only those.

`chat` is the other half of the question. A Tiiny holds text-to-speech, speech
recognition, embedding, reranking, OCR, music and image models alongside the
ones you can talk to, and every one of them is a model as far as this list is
concerned. That is the OpenAI contract and the list keeps all of them, so
`chat` is how you tell them apart: it is true for Text Generation and
Image-Text-to-Text, and false for everything else. The device itself is the
source, through the `capabilities` it reports per model, with the type as the
fallback for a row that carries none.

### `POST /v1/chat/completions`

Standard request body. Routing works like this:

0. Refuse a model that cannot chat, with a 400, before any device is touched:

   ```
   Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice is a text-to-speech model and cannot
   chat. Loaded chat models: Qwen/Qwen3-8B.
   ```

   Sent on to the device, that request comes back as the device's own
   "does not support chat" error, which reads like a broken app rather than a
   wrong choice.
1. Find the devices that have this model **loaded**. Not installed: loaded.
   Models do not auto-load on this hardware and a 35B takes tens of seconds to
   come up, so Pocket will not start one behind your back to serve a chat.
2. Among those, pick the shortest queue, then the fewest loaded models, so work
   spreads across a fleet instead of piling onto the first box.
3. Take that device's lock, make the call, release it.

The response carries `ainode_pocket.device` so you can tell which box answered.

### When it cannot serve you

A 503 that names the problem, rather than a generic failure:

```
"Qwen/Qwen3-Embedding-0.6B" is installed on TNYM26072400300011Q but is not
loaded on any of them. Models do not auto-load on this hardware: load it on the
Models page first.
```

```
no device in this fleet has a model called 'gpt-4o'. Installed across the
fleet: Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo, deepreinforce-ai/Ornith-1.0-35B
```

A 504 means the device gateway closed the request. On real firmware that happens
at about 220 seconds, measured at 222.3 s after 580 tokens with the client
timeout still at 780 s. The message says so and tells you to stream, because
streaming is the only way partial output survives that ceiling.

---

## How a device is actually reached

Two things about addressing a Tiiny are not obvious, and both were found against
real hardware rather than in a document.

### The gateway is not always on port 8800

On the firmware this was tested against, **port 8800 is closed to the LAN**. The
same service still answers on port 80 by virtual host, which is how the vendor
CLI addresses it:

```bash
curl http://192.168.100.94/api/v1/models/npu/status -H 'Host: p8800.api.tiiny'
curl http://192.168.100.94/v1/models                -H 'Host: openai.api.tiiny'
```

Two virtual hosts, split by path: `p8800.api.tiiny` for the model and NPU
endpoints, `openai.api.tiiny` for the OpenAI-compatible surface. Without a Host
header port 80 serves the device management plane.

Pocket tries the direct port first and falls back to the virtual host **only
when the direct port refuses the connection**, then remembers which one worked
and writes it to the registry, so a settled device makes exactly one attempt per
call. A 401 or a 5xx means the port is open and answering, so neither sends it
hunting for another route. The device card says which one is in use.

### A device has more than one address

Every Tiiny answers on two planes at once and reports both in its own
`device.json`:

```json
"ipv4_addresses": [
  {"interface": "usb0",  "address": "172.17.7.177"},
  {"interface": "wlan0", "address": "192.168.100.94"}
]
```

USB is a point-to-point `/30` that never changes; the Wi-Fi address is DHCP and
will move. So Pocket keeps both, prefers USB, and falls back to the LAN address
when the cable is out. Several boxes can be plugged in at once, each on its own
interface and its own `/30`, and Pocket enumerates the host's links to find them.

One wrinkle worth knowing, because it cost a measurement to find: a device
advertises its USB address whether or not the cable is in **your** machine, and
connecting to a `/30` you are not part of does not get refused, it hangs for six
seconds. So the USB plane is only tried first when this host is actually on that
link.

### Discovery

`device.json` advertises `udp_discovery_port: 39217` and
`discovery_token: "GADGET_DISCOVER_V1"`. Sending that token to that port, unicast
or broadcast, returns the whole of `device.json` in one datagram. That is the
cheapest discovery there is, it finds a box whose DHCP address has moved, and it
is what Find devices does before falling back to anything slower.

Answers are folded together by `serial_number`, so a box that replies on both
planes registers once, with both addresses.

## The lock, and why it is the point

A Tiiny runs one inference at a time. It does not batch. A second request
arriving mid-inference comes back as HTTP 200 with this in the body:

```json
{"code": 150004, "message": "The operation failed to complete."}
```

Which reads like a bad request rather than "somebody else is using it". Inside
one program you can queue your own calls. It stops being easy the moment a
second program touches the same device, because your queue and its queue have
never heard of each other.

**The design here is [OneLane](https://tiinyapp.farm/apps/onelane/)'s**, which
solved this properly first: an advisory `fcntl.flock` file in a directory both
applications can see, so the kernel releases it if a holder is killed, there is
no lease to expire and no stale state to reap. Pocket vendors that logic rather
than importing it, because it ships with the standard library alone, and it
keeps OneLane's file naming and holder record on purpose. A Pocket process and a
OneLane process pointed at the same device take out the same lock file and
genuinely take turns.

One device needs more than one of those files, and this is the part that fails
silently if you get it wrong. A Tiiny answers on USB and Wi-Fi at once, so a lock
keyed on the address gives **no exclusion at all** between a process using one
address and a process using the other. Pocket therefore takes two kinds:

- one keyed on the **serial number**, which covers every plane, and
- OneLane's **address-keyed** lock for each address the device has, so a
  neighbour that only knows about addresses is still held off.

They are taken in a fixed order, so two Pocket processes cannot deadlock against
each other.

Two limits, both OneLane's as well, and worth knowing:

- The lock is **advisory**. A program that ignores it still collides.
- `flock` coordinates processes on **one host**. Two machines pointed at one
  Tiiny share no lock file and get no mutual exclusion at all.

The Overview page says whether the shared lock is actually held, because a lock
that quietly stops being shared is worse than one that admits it.

---

## Install

### From the farm

```
farm install ainode-pocket
farm start ainode-pocket
```

The farm writes `~/.tiinyapps/device.json` (mode 0600) when you run
`farm device`, and passes `TIINY_BASE` and `TIINY_KEY` to the app. Pocket reads
its first device from there.

### From git

```
git clone https://github.com/getainode/ainode-pocket
cd ainode-pocket
python3 ainode-pocket --serve
```

There is nothing to build and nothing to install.

### Where the key comes from

Never from this repository. In order:

1. `TIINY_KEY` in the environment, which is what the farm sets.
2. `~/.tiinyapps/device.json`, the file the farm CLI writes.
3. The TiinyOS desktop app's local storage on macOS, the same lookup tiiny-bench
   uses.

A fleet has several keys and only one of them is in `~/.tiinyapps/device.json`,
so a per-device key can be stored in `~/.ainode-pocket/devices.json`, written
0600 in your home directory. Nothing is ever written into the repository, and
there is a test that fails if a key appears in it.

---

## Screenshots

Every screenshot below was taken against `--fake 2`. See the hardware note at the
bottom.

### Models

Installed models and the catalog for one device, with the NPU budget above them.
Units are memory residency, not a compute reservation: several models can be
resident at once, only one of them runs at a time.

![Models](docs/screenshots/models.png)

### Chat

The chat page talks to `/api/chat` on this app, which is `/v1/chat/completions`
with the device's own measurements carried back beside the answer. Under every
reply is what it cost: time to first token, decode tokens per second, total wall
time, tokens in and out, the device and model that served it, and the stop
reason. A `length` there is not a failure, it is the answer running into
`max_tokens`, and on a reasoning model with a modest cap it is the ordinary case.

The card on the left is the model in the picker: what it is, how big it is, how
many NPU units it takes, which devices have it loaded. The rail on the right is
every model loaded across the fleet with its unit cost and an unload button, and
below it a panel to load a chat model onto a device.

A reasoning model's chain of thought is shown separately from the answer, in a
block you can collapse, because it is spending the same `max_tokens` budget the
answer needs. The Thinking toggle decides whether any is asked for: only
`chat_template_kwargs.enable_thinking` actually turns it off on this firmware,
which is what that toggle sends. The gateway's own OpenAPI document declares a
top level `enable_thinking` beside `thinking_enabled`, `reasoning_effort` and
`thinking_budget_tokens`, and the runtime ignores every one of them.

The picker lists only models that can chat and are loaded right now, grouped by
the device holding them, and it defaults to the first of those. A box commonly
has an embedding model and a speech model loaded next to one chat model, and
offering those is offering a conversation that cannot happen. When nothing that
can chat is loaded, the page says so and points at the Models page rather than
showing an empty picker.

![Chat](docs/screenshots/chat.png)

### Bench

tiiny-bench, embedded. Prefill scaling against prompt length, sustained
generation, concurrency, and what a reasoning model charges in wall time.
Nothing is loaded, unloaded or deleted: it benchmarks something already
running, which is what makes it safe on a box doing real work.

Pick the device and the model. The model list is the loaded models on that
device that can chat, by the same rule the Chat page uses, because every
section of this suite is a chat completion and an embedding model fails all of
them. When nothing on a device can be benchmarked the Run button is disabled
and the page says so. `POST /api/bench` refuses a model that cannot chat with
the same 400 the endpoint gives, before a single section runs, so a run that
could only fail never reaches the saved results.

Requests take their turn through the device lock, so the concurrency rows
measure queueing rather than collisions. The shape to look for is aggregate
throughput staying flat while wall time doubles with each level, which is the
signature of a strict serial queue rather than a batching scheduler.

![Bench](docs/screenshots/bench.png)

---

## Command line

```
python3 ainode-pocket --serve                      web app and endpoint on 0.0.0.0:8430
python3 ainode-pocket --serve --fake 2             two fake devices, no hardware
python3 ainode-pocket --discover                   find every Tiiny this host can see
python3 ainode-pocket --discover --add             find them and register them all
python3 ainode-pocket --device 192.168.100.70      register one by address and exit
python3 ainode-pocket --discover 192.168.100.70    read /device.json from one address
python3 ainode-pocket --scan 192.168.100           also probe a /24, for a network
                                                   that drops broadcast
python3 ainode-pocket --bench --device <id> --label first-run
python3 ainode-pocket --results                    list saved benchmark results
python3 ainode-pocket --selfcheck                  offline check, no hardware
```

`--serve` binds `0.0.0.0` so another machine on the LAN can reach the page. Use
`--host 127.0.0.1` if you would rather it did not.

## Tests

```
python3 -m unittest discover -s tests
```

224 tests, no hardware and no network beyond loopback. They run against a fake
device that reproduces the recorded response shapes, and each assertion in
`tests/test_fake.py` names the artefact its shape came from. The fake also
reproduces both failures that matter, so the code has actually met them: the
in-band 150004 collision, and the gateway's per-request ceiling.

The test this project exists for is in `tests/test_gateway.py`: eight callers
arrive at one device at the same time, all eight get answers, and the device
records zero collisions. Its control case fires the same load straight at the
device with no lock and asserts that it does collide, so the first test is
measuring the lock rather than a device that never had the problem.

---

## Hardware validation

First contact with a real device was **2026-09-13**, against a Tiiny AI Pocket
Lab (serial `TNYM26072400300011Q`, TiinyOS 0.1.34) on the LAN. Everything below
was exercised **read-only**: nothing was downloaded, loaded, unloaded or deleted,
and no device setting was changed.

### What the device confirmed

| Check | Result |
|---|---|
| Discovery by UDP broadcast | found it in one packet, with both planes |
| `device.json` field names | `serial_number`, `discovery_token`, `usb.network`, `ipv4_addresses` all as expected |
| Key discovery from TiinyOS local storage | one candidate, verified live before use |
| Port 8800 direct | **refused**, in 0.05 s |
| Port 80 with `Host: p8800.api.tiiny` | 200 |
| Port 80 with `Host: openai.api.tiiny` on `/v1/models` | 200 |
| `/api/v1/sys/status` on port 80, no header | 200 |
| Transport fallback | resolved to the virtual host and was written to the registry |
| Both planes recorded, LAN chosen | the USB cable was not in this machine, and Pocket detected that rather than hanging on it |
| NPU units, memory, storage, CPU, running models | all present and matching the device |
| `/v1/models` through Pocket | 20 models, 1 ready |
| 503 for an installed but unloaded model | named the device holding it |
| 503 for a model the fleet has never heard of | listed what the fleet does have |
| Routing to the loaded model | reached the device; its own error came back attributed to it |
| Lock files | one per device, named by serial, with the address lock alongside |

### What it corrected

- **`/api/v1/models/storage` was wrong.** The real response wraps everything in a
  `{"success": true, "data": {...}}` envelope; the version inferred from prose
  did not. Nothing in Pocket reads that endpoint, which is exactly why the
  mistake could sit there unnoticed.
- **`device_info` carries no serial.** The serial comes from `device.json`, and
  that is what the device is registered under.
- **NPU utilisation is not always stuck at zero.** It read 12.48% on 0.1.34,
  where the earlier bug report had it pinned at 0.0 on 0.1.33. The card now only
  carries the caveat when the number is actually zero.
- **The NPU memory pool reported 14543 MB**, not the 49024 MB recorded against
  0.1.33. Worth re-checking before anyone quotes a pool size.

### Three shapes that are still not verified

| Shape | Based on | What breaks if it is wrong |
|---|---|---|
| `POST /api/v1/models/{id}/download/stream`, the SSE frame body | the `get_progress` fields plus `speed_human`, a real `OpenAIModel` field | the download bar shows status text instead of a percentage; the download itself is unaffected. Confirming it means starting a download, which is a write |
| `GET /api/v1/models/{id}/get_progress` while a download is running | confirmed for a model already on disk (`model_id`, `fullname`, `status`, `progress`); the downloading case is inferred | the progress readout only |
| `devices[].temp_c` and `power_w` in `/api/v1/npu/status` | declared by the spec, confirmed present and null on live firmware | nothing: the card says the firmware reports no temperature |

### Still to do on hardware

These need a write, a second box, or a cable, so they were left alone:

| # | Check | Expected |
|---|---|---|
| 1 | Load a small model (the 0.6B embedding model, 1 unit) | appears in the running list within seconds |
| 2 | Load something that does not fit the remaining budget | **measured 2026-09-14, and this expectation was wrong**: accepted with the same 200, shown as `loading`, then silently rolled back. Pocket refuses it before the device sees it |
| 3 | Unload it | disappears; the response carries `removed_container_ids` |
| 4 | Download from the catalog | progress advances and the SSE stream ends at 100 |
| 5 | Delete a loaded model | refused with 409, as the spec declares |
| 6 | Chat against a model that supports chat | answers, and names the device that served it |
| 6b | Chat against a loaded text-to-speech model | 400 before the device is touched, naming the loaded chat models |
| 6c | Benchmark a loaded embedding model | 400 in the same words; no run starts and nothing is saved |
| 7 | Eight concurrent callers (`--bench`, concurrency test) | all served, zero 150004, aggregate flat at about 24 tok/s |
| 8 | Run OneLane against the same device while Pocket is busy | it waits rather than colliding |
| 9 | Ask for a very long answer, non-streaming | 504 at about 220 s with the ceiling explained |
| 10 | The same request with `stream: true` | tokens keep arriving past that point |
| 11 | Plug the box in over USB | the card switches to the USB plane on its own |
| 12 | Two or more boxes | `/v1/models` unions them and routing reaches each |
| 13 | Pull the network on one box | that card goes offline with a reason; the others keep serving |

Rows 7, 9 and 10 are the ones most likely to differ from the fake, because they
are the ones that depend on real NPU timing.

## Layout

```
ainode-pocket          the CLI and the entry point the farm runs
pocket/device.py       one device: encoding, auth, error shapes, lifecycle, SSE
pocket/fleet.py        registry, per-device lock, telemetry, routing
pocket/gateway.py      /v1/models and /v1/chat/completions
pocket/server.py       the HTTP server, the JSON API, static assets
pocket/bench.py        tiiny-bench, embedded
pocket/fake.py         a fake device built from the recorded artefacts
web/                   the page: one html, one css, one js
tests/                 224 tests, no hardware
manifests/             the tiinyapp.farm manifest
```

Apache 2.0. Made in Texas.
