# Personal mail automation

Postbode webhook receiver and optional worker. Processing, email and calendar actions stay disabled during Railway setup. See **RAILWAY.md** for deployment instructions.

## Implemented workflow

Postbode v2 webhook -> signature and recipient validation -> durable SQLite queue ->
original PDF and OCR preserved -> structured AI extraction -> Google Drive archive ->
optional private calendar deadline -> optional email alert to the configured owner.

- Original PDFs are filed in `Personal mail / year / category` in Drive.
- Local records retain the original event, OCR, structured analysis and processing state.
- `export` produces a searchable CSV register with Drive links, dates, amounts and actions.
  A live Google Sheet register is not implemented.
- Dates and amounts carry supporting quotations. Missing evidence or invalid dates trigger review.
  Relative or ambiguous deadlines must be reviewed. Extraction still requires a real-letter pilot.
- Reply drafts appear in the alert for review. No reply is sent to a letter's sender.
- Letters requiring action or review are alerted. Clearly non-actionable routine mail is archived.
  Tax, legal, medical and bank letters also generate an alert.
- Missing OCR is flagged for manual review. This release does not run a second OCR engine.
- A supplied PDF checksum must match. Invalid or legacy payloads are rejected.
- Duplicate deliveries are deduplicated by the Postbode item UUID and PDF digest.
  A changed PDF is preserved as a new version, which may produce a new reminder.
- Drive uploads reserve IDs before upload; calendar IDs are deterministic. Email delivery
  with an uncertain outcome pauses for reconciliation instead of being automatically resent.

## Verification completed

15 application tests pass with synthetic data and mocked OpenAI/Google behavior. They cover
signature validation, wrong recipients, checksums, legacy format rejection, repeated delivery,
document revisions, durable originals after AI failure, full pipeline execution, uncertain
dates, missing OCR, ambiguous email delivery, Drive and Calendar retries, body limits,
CSV formula escaping and strict extraction schema. Six additional deployment tests cover
paused setup, the assigned port, configuration gates, the volume gate and process supervision.

**Not yet verified:** live model access or extraction quality, Google OAuth consent and live
writes, a real Postbode delivery/signature, Docker image build, public HTTPS hosting, backups
or operational monitoring. No background automation is currently running.

## Activate after the connections are ready

1. Enter the user-created OpenAI key privately into Railway's sealed `OPENAI_API_KEY` variable.
2. Select a persistent Linux host with Docker, HTTPS ingress and a backed-up volume.
   This is a continuously running receiver and worker, not a static site or scheduled chat task.
3. Create a Google OAuth desktop client for the personal Google account, enable Drive and
   Gmail APIs, and authorize locally with `authorize_google.py`. Enable Calendar API and use
   `--calendar` only once the personal destination has been selected. The authorization file
   is written with restrictive permissions and is never included in the source archive.
4. Provide configuration based on `.env.example`. Set the actual Postbode recipient UUID and
   webhook signing secret securely. Keep `ENABLE_EMAIL=false` and `ENABLE_CALENDAR=false`
   during the pilot. Google authorization must match `ALERT_EMAIL`.
5. Leave `GOOGLE_DRIVE_FOLDER_ID` blank to let the service create its private archive folder.
   A manually specified folder must be accessible to this OAuth app under the `drive.file` scope.
6. Deploy both Compose services on the same host and volume, with one worker. Put HTTPS
   in front of `127.0.0.1:8080`; allow a 40 MiB request body. Run as the provided non-root user
   and ensure the mounted OAuth token file is readable by UID 10001 but not publicly readable.
7. In Postbode, configure **webhook v2** for the intended personal recipient, the exact signing
   secret, and `https://YOUR-HOST/webhooks/postbode`. The URL is a configuration template,
   not a deployed endpoint. A Postbode outbound-post API key is not used by this receiver.
8. Send one real test letter/event. Verify PDF integrity, signature encoding, extraction and
   Drive filing. Compare every extracted date and amount to the original. Then explicitly
   enable email and the selected personal calendar, and retry the pilot record if needed.

The signature implementation expects the conventional hexadecimal HMAC-SHA256 header.
The specification names the algorithm but does not show a concrete header value, so confirm
the encoding in the first delivery before switching real mail over. Webhook v1 is unsupported.

## Local commands

Python 3.12+, Linux or macOS. Install `requirements.txt` in an isolated environment.
Supply configuration as environment variables, or use Compose's env-file loading. Compose
defaults to `../.env.local`, the approved parent-workspace destination; set `MAILROOM_ENV_FILE`
only when a different deployment destination has been selected. Python does not load env files
automatically.

```sh
python -m unittest -v test_mailroom
python authorize_google.py --client credentials.json --output token.json
docker compose up -d --build
docker compose exec worker python mailroom.py status
docker compose exec worker python mailroom.py export --output /data/mail-register.csv
docker compose exec worker python mailroom.py retry --id RECORD_ID
```

For personal calendar access, authorize with `--calendar` and set `GOOGLE_CALENDAR_ID`
to the selected personal calendar ID. Events are private, all-day and do not invite attendees.
They request email notification three days before and a popup one day before. Past or near-term
deadlines can have reminder times already elapsed; the immediate email alert remains essential.

Check `status` and failed records routinely. Failed work remains stored, with exponential retry
delays and a limit of 10 processing attempts. `/healthz` reports receiver liveness only, not model
availability or queue health. This release does not contain external uptime/failed-job monitoring.
Back up the SQLite database and original files on the persistent volume together. Stop the
services or use a proper SQLite online backup; do not copy an active database without its WAL.

If email delivery is uncertain, locate the exact `Message-ID` in Gmail Sent:
`mailroom-RECORD_ID@mailroom.local`. After verifying the outcome, run one of:

```sh
python mailroom.py reconcile-email --id RECORD_ID --email-id VERIFIED_GMAIL_MESSAGE_ID
python mailroom.py reconcile-email --id RECORD_ID --confirmed-not-sent
```

The second command is only for a confirmed non-delivery. It authorizes a new send attempt.
Changing flags does not retroactively reprocess records; use `retry` for the selected record.

## Sources

- [Postbode v2 API documentation](https://postbode.app/docs/api), fetched 1 September 2026.
  The embedded OpenAPI specification defines the inbound payload, `Signature` header,
  recipient UUID, base64 PDF, OCR `content`, document checksum and v1/v2 distinction.
  A receiver should acknowledge with 2xx; Postbode documents up to 10 retries otherwise.
- [Postbode Post API overview](https://www.postbode.nu/post-api/).
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
  The implementation uses the Responses API with a strict JSON schema and `store=false`.
- [Google Drive generated file IDs](https://developers.google.com/workspace/drive/api/guides/create-file).
- [Google Calendar events](https://developers.google.com/workspace/calendar/api/v3/reference/events).

## File contents

`mailroom.py`: receiver, queue, extraction, adapters and operator commands.
`authorize_google.py`: personal Google OAuth consent helper.
`test_mailroom.py`: offline behavioral tests.
`Dockerfile` and `compose.yaml`: deployment configuration.
`.env.example`: non-secret configuration template.

No real correspondence, Google tokens, webhook secrets, OpenAI keys, or runtime databases
are included in this package.
