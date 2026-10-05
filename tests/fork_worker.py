import os
import socket
import time

with socket.socket(socket.AF_UNIX) as server:
    while True:
        try:
            server.connect(os.environ["TESTS_FORK_SERVER"])
            break
        except (FileNotFoundError, ConnectionRefusedError):
            time.sleep(0.005)
    socket.send_fds(server, [b"w"], [0, 1, 2])
    server.recv(1)
