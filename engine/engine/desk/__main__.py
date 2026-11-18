"""`python -m engine.desk`: serve the Factory Desk on 127.0.0.1 at engine.toml's `desk_port` until stopped."""

from __future__ import annotations

import sys

from engine import settings as settings_mod
from engine.desk.server import Desk, make_server
from engine.factory import install


def main() -> int:
    port = settings_mod.load().desk_port
    install.desk_token()  # made now, so `factory engine status` and the desk agree from the first visit
    desk = Desk()
    try:
        server = make_server(desk, port)
    except OSError as e:
        desk.close()
        print(f"factory desk: can't listen on 127.0.0.1:{port}: {e.strerror} (desk_port in engine.toml)", file=sys.stderr)
        return 1
    print(f"factory desk: serving on 127.0.0.1:{port}; open the link `factory engine desk` prints", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        desk.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
