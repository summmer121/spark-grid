# Security Policy

## Reporting

Please report vulnerabilities through GitHub Security Advisories for this repository.

Do not include passwords, API tokens, private keys, session cookies, database files, or unredacted production logs in public issues.

## Deployment Baseline

- Use HTTPS for the web console.
- Set `SPARK_COOKIE_SECURE=1` when serving over TLS.
- Restrict SSH and monitoring ports to trusted management networks.
- Run the service as an unprivileged user.
- Use a dedicated SSH key with limited access.
- Never commit `.env`, `data/`, logs, databases, or private keys.
