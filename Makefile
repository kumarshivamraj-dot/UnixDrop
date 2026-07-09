.PHONY: up status health tab tab-no-open tab-browser run-mac run-linux test compile check-shell package-smoke ci clean-artifacts migrate-config

PYTHON ?= python3

up:
	./deskbridge up

status:
	./deskbridge status

health:
	./deskbridge health

tab:
	./deskbridge tab

tab-no-open:
	./deskbridge tab --no-open

# Usage: make tab-browser BROWSER=chrome
# Supported values: auto safari chrome arc brave chromium edge firefox firefox-developer librewolf vivaldi opera
BROWSER ?= auto
tab-browser:
	./deskbridge tab --browser $(BROWSER)

run-mac:
	./scripts/run_mac_agent.sh

run-linux:
	./scripts/run_linux_receiver.sh

test:
	$(PYTHON) -m unittest discover -s tests -v

compile:
	$(PYTHON) -m compileall -q unixdrop tests

check-shell:
	bash -n deskbridge scripts/*.sh

ci: compile check-shell test package-smoke

package-smoke:
	tmpdir="$$(mktemp -d)"; \
	trap 'rm -rf "$$tmpdir" build unixdrop.egg-info' EXIT; \
	$(PYTHON) -m venv --system-site-packages "$$tmpdir/venv"; \
	"$$tmpdir/venv/bin/python" -m pip install --no-build-isolation .; \
	"$$tmpdir/venv/bin/deskbridge" --help >/dev/null; \
	"$$tmpdir/venv/bin/deskbridge" setup --help >/dev/null; \
	"$$tmpdir/venv/bin/deskbridge" tab --help >/dev/null; \
	"$$tmpdir/venv/bin/deskbridge" health --help >/dev/null; \
	"$$tmpdir/venv/bin/deskbridge" doctor --help >/dev/null; \
	"$$tmpdir/venv/bin/deskbridge" init --config "$$tmpdir/config.json" >/dev/null; \
	test -s "$$tmpdir/config.json"

clean-artifacts:
	rm -rf build dist unixdrop.egg-info unixdrop/__pycache__ tests/__pycache__ scripts/__pycache__

migrate-config:
	$(PYTHON) ./scripts/migrate_config.py --path ~/.config/unixdrop/config.json
