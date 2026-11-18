"""The Factory Desk: one page on the home machine to see what the factory is doing (docs/designs/desk.md).

    python -m engine.desk          serve it on 127.0.0.1:<desk_port> (engine.toml, default 8245)

`server.py` holds the request path and its safety checks, `pages.py` the HTML, `repos.py` the reads beyond the
engine (a repo's factory pin, CI and factory PRs). Everything the engine knows comes from `engine.ops`, the read the
CLI shares. Standard library only on top of the engine's own dependencies.
"""
