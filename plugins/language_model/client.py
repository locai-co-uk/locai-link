# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import argparse
import socket
import sys
import threading

import colorama
from colorama import Fore, Style

colorama.init()


def recvall(sock, n):
    """Helper to ensure we receive exactly n bytes (handling TCP fragmentation)."""
    data = b""
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            return None
        data += packet
    return data


def receive_loop(sock):
    """Listens for data from the Agent and prints it.

    Args:
        sock: The socket connection.
    """
    while True:
        try:
            # 1. Read message length (4 bytes)
            len_bytes = recvall(sock, 4)
            if not len_bytes:
                break

            # 2. Read message body
            msg_len = int.from_bytes(len_bytes, "big")
            data = recvall(sock, msg_len)
            if not data:
                break

            # 3. Print
            print(data.decode("utf-8", errors="replace"), end="", flush=True)
        except Exception:
            break

    print(f"\n{Fore.YELLOW}[Connection Closed by Agent]{Style.RESET_ALL}")
    sock.close()
    sys.exit(0)


def main():
    """Entry point for the remote chat client."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    HOST = "127.0.0.1"
    print(f"Connecting to Agent Chat on {HOST}:{args.port}...")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((HOST, args.port))

        # Start background listener
        t = threading.Thread(target=receive_loop, args=(s,), daemon=True)
        t.start()

        # Main Input Loop
        while True:
            try:
                # Get raw input
                text = input()
                # Send to Agent
                data = text.encode("utf-8")
                # Send length-prefixed message
                s.sendall(len(data).to_bytes(4, "big") + data)
            except EOFError:
                break

    except ConnectionRefusedError:
        print(f"{Fore.YELLOW}Could not connect. Is the Agent running?{Style.RESET_ALL}")
        input("Press Enter to exit...")
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        input("Press Enter to exit...")


if __name__ == "__main__":
    main()
