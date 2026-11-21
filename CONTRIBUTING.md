# Contributing

1. Create a feature branch.
2. Keep changes focused and include tests where behavior changes.
3. Run the test suite before opening a pull request.
4. Do not include real hostnames, public IP addresses, credentials, logs, databases, or screenshots containing personal infrastructure.

```bash
python -m unittest discover -s tests -v
node --check app/static/js/app.js
```

Use RFC 5737 documentation addresses, such as `192.0.2.10`, in examples and tests.
