.PHONY: status health tab tab-no-open tab-browser run-mac run-linux test migrate-config

status:
	./deskbridge status

health:
	./deskbridge health

tab:
	./deskbridge tab

tab-no-open:
	./deskbridge tab --no-open

# Usage: make tab-browser BROWSER=chrome
# Supported values: auto safari chrome arc brave chromium edge vivaldi opera
BROWSER ?= auto
tab-browser:
	./deskbridge tab --browser $(BROWSER)

run-mac:
	./scripts/run_mac_agent.sh

run-linux:
	./scripts/run_linux_receiver.sh

test:
	python3 -m unittest discover -s tests -v

migrate-config:
	python3 ./scripts/migrate_config.py --path ~/.config/unixdrop/config.json
