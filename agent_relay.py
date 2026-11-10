"""Lend your ssh agent to the launcher's container: listen on a unix socket in a folder the container sees (on a disk
shared with the host, e.g. /data) and connect each client to the agent's socket (in this machine's /tmp, which the
container does not see). Started and stopped by start.sh.

    agent_relay.py LISTEN_SOCKET AGENT_SOCKET

The listening socket is created with mode 600, in a folder start.sh makes with mode 700. Runs until killed.

When the agent stops answering (the VS Code window it came from was closed: VS Code gives each window its own
/tmp/vscode-ssh-auth-<id>.sock; or the ssh -A session: /tmp/ssh-<id>/agent.<pid>), the other agents of those kinds are
tried, newest first, and the first one that answers is used from then on. start.sh agent points it at a given one.
"""
import glob
import os
import socket
import struct
import sys
import threading
import time


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


class Agent:
    """The agent's socket: the one given, else (when its agent does not answer) another one of the same kind. A socket
    can accept connections with no agent behind it (VS Code keeps the socket of a closed window): an agent answers
    "list your keys" (SSH_AGENTC_REQUEST_IDENTITIES, 11) with the list (SSH_AGENT_IDENTITIES_ANSWER, 12)."""
    CHECK_S = 30                                            # an agent that answered is trusted that long

    # the agents a relay may fall back on: VS Code's (one per window) and OpenSSH's forwarded ones (ssh -A: one per
    # session), of this user (the others' cannot be opened)
    PATTERNS = ("/tmp/vscode-ssh-auth-*.sock", "/tmp/ssh-*/agent.*")

    def __init__(self, path):
        self.path, self.checked = path, 0.0

    @staticmethod
    def _answers(path):
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(3)
        try:
            s.connect(path)
            s.sendall(struct.pack(">IB", 1, 11))
            head = s.recv(5)
            return len(head) == 5 and head[4] == 12
        except OSError:
            return False
        finally:
            s.close()

    def _open(self, path):
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(path)
        except OSError:
            s.close()
            return None
        return s

    def connect(self):
        """A connection to an agent that answers (the current one when it does), or None."""
        if time.time() - self.checked < self.CHECK_S or self._answers(self.path):
            self.checked = time.time()
            return self._open(self.path)
        others = [p for pat in self.PATTERNS for p in glob.glob(pat) if p != self.path]
        others.sort(key=lambda p: os.stat(p).st_mtime if os.path.exists(p) else 0, reverse=True)
        for p in others[:400]:
            if self._answers(p):
                print(f"agent_relay: the agent of {self.path} does not answer: using {p}", file=sys.stderr, flush=True)
                self.path, self.checked = p, time.time()
                return self._open(p)
        return None


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
    path, agent = argv[0], Agent(argv[1])
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
        upstream = agent.connect()
        if upstream is None:                                # no agent answers: the client sees it fail
            client.close()
            continue
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
