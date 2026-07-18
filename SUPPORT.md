# Support

Agent is a maintained self-hosted open-source project, not a hosted service.

## What to expect

- There is no support SLA, compatibility promise, or guaranteed response time.
- Reproducible bug reports and focused pull requests are welcome.
- Operators are responsible for their own security review, cloud costs, dependency updates, incident response, backups, and platform compatibility.

Before opening an issue, run:

~~~bash
make doctor
make setup
make check
~~~

For guided-deployment problems, also run:

~~~bash
make self-host SELF_HOST_ARGS="--dry-run --domain agent.example.com --region us-west-2"
~~~

Include the failing command, relevant redacted output, operating system, Python version, Node.js version, and the commit you tested.

Do not post credentials, customer data, private URLs, AWS account identifiers, or undisclosed vulnerabilities. Follow [SECURITY.md](./SECURITY.md) for security reports.
