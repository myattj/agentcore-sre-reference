# Support

Agent is an archived reference implementation, not a hosted service or actively maintained product.

## What to expect

- There is no support SLA, compatibility promise, roadmap, or guaranteed response time.
- Issues and pull requests may still help future readers when they document a reproducible problem or improve the repository as a reference.
- Operators of forks are responsible for their own security review, cloud costs, dependency updates, incident response, and platform compatibility.

Before opening an issue, run:

~~~bash
make doctor
make setup
make check
~~~

Include the failing command, relevant redacted output, operating system, Python version, Node.js version, and the commit you tested.

Do not post credentials, customer data, private URLs, AWS account identifiers, or undisclosed vulnerabilities. Follow [SECURITY.md](./SECURITY.md) for security reports.
