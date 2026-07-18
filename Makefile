.PHONY: help setup doctor check demo

help:
	@printf '%s\n' \
	  'Agent local commands:' \
	  '  make setup   Install every locked dependency and create safe local env files' \
	  '  make doctor  Check required and optional developer tools' \
	  '  make demo    Run the bridge + web UI with a no-cloud incident dashboard' \
	  '  make check   Run the same local validation gates used by CI'

setup:
	@./scripts/setup.sh

doctor:
	@./scripts/doctor.sh

check:
	@./scripts/check.sh

demo:
	@./scripts/demo.sh
