# Security Policy

Use the latest release and review Dependabot updates before deployment.

Report vulnerabilities through this repository's GitHub Security Advisories
private reporting feature when enabled. Do not put tokens, database passwords,
or member data in public issues. If private reporting is unavailable, ask the
maintainer for a private contact channel without disclosing the vulnerability.

Rotate exposed Discord tokens immediately in the Discord Developer Portal.
Rotate database credentials separately. Removing a secret from the latest commit
does not remove it from Git history or invalidate it.

Tags, tag files, stored TagScript variables, the tag use log (which member ran which
tag and when, kept for one year), and Discord IDs are user data. Restrict
database and backup access, define a backup retention policy, and do not expose
PostgreSQL to the public internet. Uploaded files are stored as-is and are not
scanned for malware; treat them as untrusted. TagScript never runs Python code, and
the bot only downloads from Discord's CDN hosts, without following redirects. Keep
Discord and database credentials out of screenshots and logs.
