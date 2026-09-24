# Screenshots

Taken at: 0.1.4

These four are what tiinyapp.farm shows on the listing, in the order the
manifest names them, and what the README embeds. They are the only pictures of
this app anyone sees before they install it, so they have to be the app as it is
now rather than the app as it once was.

That went wrong once already. The 0.1.0 shoot stayed in place through 0.1.1,
0.1.2 and most of 0.1.3, so the chat picture still showed a single column with no
numbers in it long after the chat page had grown three columns and a stats bar,
and three of the four still carried the tagline the footer retired in 0.1.1. A
version bump is the moment to look at these again, which is why the line at the
top says which version they were taken at and a test compares it to the version
in `pocket/__init__.py`. Re-shoot them, or change the line on purpose.

## How they were taken

One throwaway fleet, no hardware, 1280x900 each:

```
python3 ainode-pocket --serve --fake 2 --host 127.0.0.1 --port 8440
```

Chrome addresses the app on 8430, the port the README and the manifest
document, and the resolver sends the connection to the throwaway instance. The
page prints whichever host it was reached on, so this is how the endpoint box
reads on an ordinary install rather than how it read on the machine that took
the picture.

```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --hide-scrollbars --window-size=1280,900 \
  --virtual-time-budget=8000 --user-data-dir="$(mktemp -d)" \
  --host-resolver-rules="MAP 127.0.0.1:8430 127.0.0.1:8440" \
  --screenshot=overview.png "http://127.0.0.1:8430/?view=overview"
```

- `overview.png`, `models.png`: `?view=overview` and `?view=models`.
- `bench.png`: two runs of the suite against the fakes first, so the page has a
  finished run to show and a history to list. `POST /api/bench` is what the Run
  button does.
- `chat.png`: `?demo=1`, the fixture the page carries for exactly this purpose.
  Every number in that picture is the fixture's, and the model card says so on
  screen. A live shot was the other option and it reads worse: the only figure
  the browser measures itself, the thinking duration, collapses to zero under
  headless virtual time, which would put a wrong number on the listing.

Headless Chrome did not exit on its own on any of these runs, so each one was
backgrounded under a sixty second guard that killed it. The file was complete
every time.
