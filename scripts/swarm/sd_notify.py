"""The systemd notify protocol, so a service that hangs without exiting misses its watchdog and is restarted."""

import os
import socket


def notify(message: str, environ=os.environ) -> bool:
    address = environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        try:
            sock.sendto(message.encode(), address)
        except OSError:
            return False
    return True
