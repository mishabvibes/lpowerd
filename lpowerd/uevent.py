"""Kernel uevent listener (NETLINK_KOBJECT_UEVENT) without pyudev.

The daemon sleeps in poll() on this socket; AC plug/unplug and battery
status changes wake it, so no timers are needed while idle.
"""

import socket

NETLINK_KOBJECT_UEVENT = 15


def open_socket():
    s = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK,
                      NETLINK_KOBJECT_UEVENT)
    s.bind((0, 1))  # pid 0 = kernel assigns; group 1 = kernel broadcasts
    return s


def parse(msg):
    parts = msg.split(b"\0")
    env = {}
    for p in parts[1:]:
        k, sep, v = p.partition(b"=")
        if sep:
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def drain(sock):
    """Yield parsed events until the socket would block."""
    while True:
        try:
            msg = sock.recv(16384)
        except BlockingIOError:
            return
        if msg.startswith(b"libudev"):
            continue
        yield parse(msg)
