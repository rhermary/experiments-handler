"""Lend your ssh agent to the launcher's container: listen on a unix socket in a folder the container sees (on a disk
shared with the host, e.g. /data) and connect each client to the agent's socket (in this machine's /tmp, which the
container does not see). Started and stopped by start.sh.

    agent_relay.py LISTEN_SOCKET AGENT_SOCKET

The listening socket is created with mode 600, in a folder start.sh makes with mode 700. Runs until killed.
"""
import os
import socket
import sys
import threading


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def main(argv):
    """Entry point.

    Args:
        argv: the listening socket and the agent's socket.

    Returns:
        The exit code (2 for a bad command line; otherwise it runs until killed).
    """
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path, agent = argv
    if os.path.exists(path):
        os.unlink(path)
    listener = socket.socket(socket.AF_UNIX)
    old = os.umask(0o177)
    try:
        listener.bind(path)
    finally:
        os.umask(old)
    listener.listen(16)
    while True:
        client, _ = listener.accept()
        upstream = socket.socket(socket.AF_UNIX)
        try:
            upstream.connect(agent)
        except OSError:
            client.close()
            upstream.close()
            continue
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
