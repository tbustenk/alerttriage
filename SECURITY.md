# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 2.x     | ✅ Active  |
| 1.x     | ❌ End of life |

---

## Reporting a vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

If you discover a security issue, please report it privately:

1. **Email:** Send details to the maintainer contact listed in the repository's GitHub profile, or open a [GitHub Security Advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing/privately-reporting-a-security-vulnerability) (Repository → Security → Advisories → "Report a vulnerability").
2. **Include:**
   - A description of the vulnerability and its potential impact
   - Steps to reproduce (proof-of-concept if applicable)
   - Affected version(s)
   - Any suggested mitigations you are aware of
3. **Do not** include actual client data, API keys, or production credentials in your report.

### Response timeline

| Stage | Target |
|---|---|
| Acknowledgement | Within 48 hours |
| Initial assessment | Within 5 business days |
| Fix or mitigation | Within 30 days for critical issues |
| Public disclosure | Coordinated with reporter |

We follow [responsible disclosure](https://en.wikipedia.org/wiki/Responsible_disclosure): we ask reporters to give us reasonable time to fix the issue before making it public.

---

## Security considerations for self-hosting

AlertTriage handles security alert data and communicates with external AI APIs. If you deploy it, review the following:

### API keys

- Store all API keys in environment variables, never in YAML config files or source code.
- Use the `.env.example` template; never commit `.env` to version control.
- Rotate SIEM credentials on the schedule your organisation requires. Use the per-client env-var pattern (`ALERTTRIAGE_<CLIENT_ID>_API_KEY`) so rotation requires no code changes.

### PII and data residency

- The anonymization layer (`src/anonymize.py`) strips IPs, usernames, card numbers, SSNs, and API keys before any data is sent to an AI provider.
- **Always set `anonymize: true` in production client configs.** Setting it to `false` sends raw alert data to the AI provider's infrastructure.
- If your organisation has data residency requirements (GDPR, HIPAA, etc.), verify that your chosen AI provider's data processing agreement covers your region and data classification before enabling a backend.
- The `reverse_map` (pseudonym → original value) is held only in process memory and is never persisted or logged.

### Network security

- SIEM connectors verify TLS by default (`verify_ssl: true`). Do not disable this in production.
- The webhook output connector signs payloads with HMAC-SHA256 when a `secret` is configured. Always configure a secret for production webhook endpoints.

### Authentication

- AlertTriage itself has no built-in authentication layer — it is a library/CLI, not a public-facing service.
- If you expose it as a service (e.g., via the HTTP bridge), add authentication at the network or reverse-proxy layer.

### Cost and rate limits

- Set `cost_limits.daily_usd` and `cost_limits.monthly_usd` in every client config to prevent runaway spend if an input pipeline misbehaves.
- AI provider rate limits are not enforced by AlertTriage. Configure appropriate concurrency limits (`max_concurrency` in `analyze_batch`) to avoid throttling.

### Data at rest

- Feedback databases (`data/<client_id>/feedback.db`) contain analyst notes and verdicts derived from security alerts. Apply appropriate filesystem permissions and include them in your backup and retention policy.
- Cost state (`data/<client_id>/cost.json`) is not sensitive, but the data directory as a whole should not be world-readable.

### Dependency supply chain

- Pin dependency versions in production using a lockfile (e.g., `pip-compile requirements.txt`).
- The CI workflow runs `pip-audit` on every push to scan for known CVEs in dependencies.
- Review upstream changes in `anthropic` and `openai` SDK releases before upgrading, as they control the data path to AI providers.

---

## Threat model (brief)

| Threat | Mitigation |
|---|---|
| Leaked API keys | Env vars only; `.env` in `.gitignore`; CI uses placeholder keys |
| PII reaching AI provider | Anonymizer runs before every AI call; `anonymize: true` default |
| Compromised AI response | JSON schema validation on all AI output; invalid responses logged and surfaced as `verdict=unknown` |
| Malicious SIEM payload | Raw payloads are not executed; they are serialised and passed as text to the AI prompt |
| Cost abuse | Per-client daily/monthly hard limits; enforced before every AI call |
| Runaway concurrency | `max_concurrency` parameter on `analyze_batch`; configurable per run |
