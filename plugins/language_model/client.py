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
    """Read exactly ``n`` bytes from ``sock`` (handles TCP fragmentation)."""
    data = b""
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            return None
        data += packet
    return data


def receive_loop(sock):
    """Read length-prefixed frames from the agent and print them to stdout."""
    while True:
        try:
            len_bytes = recvall(sock, 4)
            if not len_bytes:
                break
            msg_len = int.from_bytes(len_bytes, "big")
            data = recvall(sock, msg_len)
            if not data:
                break
            print(data.decode("utf-8", errors="replace"), end="", flush=True)
        except Exception:
            break

    print(f"\n{Fore.YELLOW}[Connection Closed by Agent]{Style.RESET_ALL}")
    sock.close()
    sys.exit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    HOST = "127.0.0.1"
    print(f"Connecting to Agent Chat on {HOST}:{args.port}...")

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((HOST, args.port))

        t = threading.Thread(target=receive_loop, args=(s,), daemon=True)
        t.start()

        while True:
            try:
                text = input()
                data = text.encode("utf-8")
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
