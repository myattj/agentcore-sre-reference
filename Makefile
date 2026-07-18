.PHONY: help setup doctor aws-doctor aws-configure check demo

AWS_CONFIGURE_ARGS ?=

help:
	@printf '%s\n' \
	  'Agent local commands:' \
	  '  make setup   Install every locked dependency and create safe local env files' \
	  '  make doctor  Check required and optional developer tools' \
	  '  make aws-doctor Verify the selected AWS identity, region, and AgentCore access' \
	  '  make aws-configure Verify AWS and write the ignored AgentCore deployment target' \
	  '  make demo    Run the bridge + web UI with a no-cloud incident dashboard' \
	  '  make check   Run the same local validation gates used by CI' \
	  '' \
	  'Pass AWS flags with AWS_CONFIGURE_ARGS, for example:' \
	  '  make aws-configure AWS_CONFIGURE_ARGS="--profile sandbox --region eu-west-1"'

setup:
	@./scripts/setup.sh

doctor:
	@./scripts/doctor.sh

aws-doctor:
	@./scripts/configure_aws.py --check-only $(AWS_CONFIGURE_ARGS)

aws-configure:
	@./scripts/configure_aws.py $(AWS_CONFIGURE_ARGS)

check:
	@./scripts/check.sh

demo:
	@./scripts/demo.sh
