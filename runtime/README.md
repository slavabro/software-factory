# factory-runtime

Building blocks a product installs instead of rebuilding. Design and order: `docs/designs/foundation.md`.

```
factory-runtime @ git+https://github.com/slavabro/software-factory@vX.Y#subdirectory=runtime
```

## `factory_runtime.temporal`

| Piece | Use |
|-------|-----|
| `TemporalSettings` | server, namespace, task queue, payload limits; pass your own values or `TemporalSettings.from_env()` |
| `get_client(settings)` | the process-wide client for those settings, with the shared data converter |
| `data_converter(min_bytes, warn_bytes)` | Pydantic JSON + zlib for large payloads; every client, worker and test env uses it |
| `build_workflow_runner(passthrough=…)` | the sandbox runner; the product names its own pure modules to pass through |
| `activity_idempotency_key()`, `DATA_SHAPE_ERRORS` | inside activities: a key stable across retries; errors a retry can't fix |
| `get_run_failure(client, …)` | one line on why a closed run failed |
| `signal_with_start(client, …)` | start a workflow or join its run, delivering a signal atomically |

`factory_runtime.testing`: `make_worker` (the production runner with the strict import policy), `time_skipping_env`,
`query_eventually`.

The package never reads a product's config: everything comes in as arguments. Tests: `python -m unittest discover
-s runtime/tests` with the package installed (`pip install ./runtime`).
