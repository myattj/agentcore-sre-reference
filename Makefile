.PHONY: help setup doctor aws-doctor aws-configure agent-deploy self-host check demo

AWS_CONFIGURE_ARGS ?=
AGENTCORE_DEPLOY_ARGS ?=
SELF_HOST_ARGS ?=

help:
	@printf '%s\n' \
	  'Agent local commands:' \
	  '  make setup   Install every locked dependency and create safe local env files' \
	  '  make doctor  Check required and optional developer tools' \
	  '  make aws-doctor Verify the selected AWS identity, region, and AgentCore access' \
	  '  make aws-configure Verify AWS and write the ignored AgentCore deployment target' \
	  '  make agent-deploy Validate and deploy AgentCore with the selected AWS target' \
	  '  make self-host Guided deployment into your AWS account and Slack app' \
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

agent-deploy:
	@./scripts/deploy_agent.sh $(AGENTCORE_DEPLOY_ARGS)

self-host:
	@python3.13 ./scripts/self_host.py $(SELF_HOST_ARGS)

check:
	@./scripts/check.sh

demo:
	@./scripts/demo.sh
