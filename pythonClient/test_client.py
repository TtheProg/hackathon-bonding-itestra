#!/usr/bin/env python3

import sys

import requests


def main():
    # Parse command line arguments: <name> [ip] [port]
    if len(sys.argv) < 2:
        print("Usage: python test_client.py <name> [ip] [port]")
        print("Example: python test_client.py team1")
        print("Example: python test_client.py team1 192.168.1.10 3030")
        sys.exit(1)

    name = sys.argv[1]
    ip = sys.argv[2] if len(sys.argv) > 2 else "localhost"
    port = sys.argv[3] if len(sys.argv) > 3 else "3030"

    base_url = f"http://{ip}:{port}"

    try:
        print(f"Connecting as '{name}' to {base_url}...")
        response = requests.post(
            f"{base_url}/connect",
            json={"name": name},
            timeout=5,
        )
        if response.status_code == 200:
            print(f"Connected successfully! Response: {response.json()}")
        else:
            print(f"Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"  Error: {e}")


if __name__ == "__main__":
    main()
