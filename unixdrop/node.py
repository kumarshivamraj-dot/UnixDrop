from __future__ import annotations

from unixdrop import linux_service, mac_agent


def main() -> None:
    server, _thread = linux_service.start_receiver_in_thread(start_clipboard_watcher=True)
    try:
        mac_agent.main(start_deskflow=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
