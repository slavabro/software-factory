# factory-runtime

Building blocks a product installs instead of rebuilding. Design and order: `docs/designs/foundation.md`.

```
factory-runtime @ https://github.com/slavabro/software-factory/archive/<commit>.tar.gz#subdirectory=runtime
```

Pin the same commit as the project's `slavabro/software-factory@<commit>` action, and keep a test that checks the
two are equal (FamilyOffice: `tests/standalone/test_software_factory_pin.py`). The archive URL needs no git and no
login, so it installs in slim Docker images too.

## `factory_runtime.temporal`

| Piece | Use |
|-------|-----|
| `TemporalSettings` | server, namespace, task queue, payload limits; pass your own values or `TemporalSettings.from_env()` |
| `get_client(settings)`, `cached_client`, `set_client` | the process-wide client for those settings, with the shared data converter |
| `data_converter(min_bytes, warn_bytes)` | Pydantic JSON + zlib for large payloads; every client, worker and test env uses it |
| `build_workflow_runner(passthrough=…)` | the sandbox runner; the product names its own pure modules to pass through |
| `activity_idempotency_key()`, `DATA_SHAPE_ERRORS` | inside activities: a key stable across retries; errors a retry can't fix |
| `get_run_failure(client, …)` | one line on why a closed run failed |
| `signal_with_start(client, …)` | start a workflow or join its run, delivering a signal atomically |

`factory_runtime.testing`: `make_worker` (the production runner with the strict import policy), `time_skipping_env`,
`query_eventually`.

The package never reads a product's config: everything comes in as arguments. `TemporalSettings` covers a local
server and Temporal Cloud (`tls`, `api_key`); anything else `Client.connect` takes goes through `get_client`.
`cached_client` / `set_client` read or put the process's client without connecting (health checks, tests).

**Version:** `factory_runtime.__version__`; a release is the factory tag `v<version>`.

**Tests:** `pip install ./runtime`, then `cd runtime && python -m unittest discover -s tests -t .` (CI runs them
against the installed package, from outside the source tree).
