# Railway deployment

Prepared for a single service in Amsterdam. Railway project and empty service created;
application upload, persistent volume and first deployment remain pending.

## Deployment checkpoint (1 September 2026)

- Project: `postbode-mail-automation` (`7c58a839-61f4-4744-9103-eda6baa4d2e9`).
- Environment: `production` (`7d339aab-ad46-46c2-95a3-ce817f9b860f`).
- Service: `postbode-mail` (`d1bef9f5-f57c-4a99-ac2a-90d9f458b813`).
- Dashboard: https://railway.com/project/7c58a839-61f4-4744-9103-eda6baa4d2e9
- Read-back configuration shows one replica in Amsterdam (`ams`), Dockerfile builder,
  `Dockerfile.railway`, `/healthz`, and a 120-second health-check timeout.
- Set `PROCESSING_ENABLED=false`, `ENABLE_EMAIL=false`, `ENABLE_CALENDAR=false`,
  `DATA_DIR=/data`, `OPENAI_MODEL=gpt-4.1-mini`, and `PYTHONUNBUFFERED=1` with deploys skipped.
- Requested sleeping disabled, no cron schedule and ON_FAILURE restart with 10 retries;
  the configuration read-back omits sleeping/restart fields, so verify those before deployment.
- No secrets, Google authorization or Postbode webhook connection were configured.
- No volume, source upload, deployment, public domain, backups or uptime monitor exists yet.
- All 21 offline tests passed. No live integration or cloud health check has run.
- Official CLI installation could not complete: the execution environment reported
  `network approval was cancelled before a decision was returned`.

Resume the existing project and service, do not create duplicates. The connected Railway
controls manage the project but do not upload local source files. Use an authorized CLI
upload session or connect a private source repository. Attach `/data` in Amsterdam before
the first build/deploy. Keep all three action switches false throughout setup.

### Current Railway configuration compatibility

Railway rejected setting `railway.json` on this new service because Config as Code is
deprecated for new services. The equivalent service settings above were applied directly.
The packaged `railway.json` is retained only as a legacy reference and excluded from CLI
uploads by `.railwayignore`; do not configure it as this service's configuration file.
For a Git source, omit that legacy file from the repository. The service already selects
`Dockerfile.railway`. Optional migration to `.railway/railway.ts` can follow once the CLI
is available. Current documentation: https://docs.railway.com/infrastructure-as-code

## Design

`Dockerfile.railway` runs the receiver and optional worker under `serve.py`, using the same
SQLite database and volume. If either process exits, the supervisor stops its companion
and exits unsuccessfully so Railway can restart the whole service. Data remains on the volume.
The entry process initializes volume ownership and drops root privileges before starting the app.

Attach one persistent volume at `/data`. Do not add replicas or another region. The startup
check refuses to run on Railway without the expected mounted volume. Set the region before
attaching the volume. Keep Railway's optional serverless/sleeping mode disabled.

## Account connection

Use the official Railway CLI's browserless pairing flow. This allows the user to authorize
deployment access in their own browser without sending an account password or API token in chat.
The authorization screen is the place to review the access being granted.

## Initial deployment

1. Reuse the project and single empty service from the checkpoint, then upload this directory
   using the Railway CLI or a private Git repository. The service settings select
   `Dockerfile.railway`; do not opt this new service into legacy `railway.json` configuration.
2. Select Amsterdam and attach the persistent volume at `/data` before deploying.
3. The container defaults to `PROCESSING_ENABLED=false`, `ENABLE_EMAIL=false`, and
   `ENABLE_CALENDAR=false`. With no Postbode signing configuration it rejects incoming events.
4. Confirm the build, startup volume check and `/healthz` endpoint succeed. Generate a Railway
   HTTPS domain. These checks prove hosting works, not that the external integrations work.

## Configuration in Railway

Set these in the service's Variables panel. Enter secrets yourself and seal them; do not send
them in chat. No existing OpenAI key is imported into the local workspace by this deployment.

| Variable | Value |
| --- | --- |
| `OPENAI_API_KEY` | User-created API key, sealed |
| `OPENAI_MODEL` | `gpt-4.1-mini` |
| `DATA_DIR` | `/data` |
| `POSTBODE_WEBHOOK_SECRET` | Signing secret configured at Postbode, sealed |
| `POSTBODE_RECIPIENT_UUID` | Verified personal recipient UUID |
| `ALERT_EMAIL` | Owner's verified personal Gmail address |
| `GOOGLE_TOKEN_JSON` | Dedicated Google OAuth authorization JSON, sealed |
| `GOOGLE_DRIVE_FOLDER_ID` | Optional; otherwise the app creates its archive |
| `GOOGLE_CALENDAR_ID` | Selected personal calendar only |
| `PROCESSING_ENABLED` | `false` until connection checks are complete |
| `ENABLE_EMAIL` | `false` until the pilot alert is authorized |
| `ENABLE_CALENDAR` | `false` until the personal calendar is selected |

`GOOGLE_TOKEN_JSON` replaces the local `GOOGLE_TOKEN_FILE` option for managed hosting.
It must contain `client_id`, `client_secret` and `refresh_token` from the dedicated personal
Google authorization. Connecting Google to ChatGPT does not create these service credentials.
The provided `authorize_google.py` creates that grant on the user's computer. A browser-based
hosted Google consent route is not part of this release and would need additional implementation
if setup must be completed entirely from a phone.

Run the first end-to-end test on a synthetic letter. Inspect PDF storage, amount/date extraction
and the configured alert destination. Only then point Postbode's personal v2 webhook at the real
HTTPS endpoint and enable processing. A successful deployment alone is not activation.

Enable volume backups and an external uptime monitor before relying on this for deadlines.
Railway's deployment health check is not a continuous monitor. Queue failures need monitoring
as described in README.md. Monitor storage because raw event JSON also contains the base64 PDF.

## Verification

Run `python -m unittest -v test_mailroom test_serve`.

The additional deployment tests exercise setup mode, Railway's assigned port, missing
credentials, missing volume and companion-process shutdown. Cloud build and live Google,
OpenAI and Postbode calls still require account connections and a real deployment.

Official references: [CLI login](https://docs.railway.com/cli/login),
[configuration](https://docs.railway.com/config-as-code/reference),
[volumes](https://docs.railway.com/volumes/reference),
[regions](https://docs.railway.com/deployments/regions).
