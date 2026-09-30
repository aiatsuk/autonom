# Autonom privacy policy

Last updated: September 30, 2026.

Autonom is an open-source mobile testing and debugging tool maintained by
Andrei Iatsuk. This policy covers the Autonom skills and CLI. Your AI assistant,
the applications you test, GitHub, and other services you choose have their own
privacy policies.

## Data used during testing

Autonom processes data from the devices and applications you authorize it to
test. Depending on the commands you run, this can include device and app
identifiers, timestamps, accessibility trees, screenshots, recordings, logs,
crash reports, performance measurements, application files, network request and
response details, and notes. These artifacts may contain personal information
from the app under test.

This data is used to operate the selected test target, reproduce problems,
record evidence, and produce reports. Autonom does not require an account with
the maintainer and does not send usage analytics or session artifacts to the
maintainer automatically.

## Storage and retention

Session artifacts are stored on your machine, normally under
`~/.autonom/sessions/`. Saved app knowledge normally lives under
`~/.autonom/apps/`, and supporting machine state under
`~/.local/state/autonom/`. Configuration and commands can select other local
paths, including `AUTONOM_HOME`.

There is no automatic expiration period for these local files: they remain
until you delete them. Stopping a session stops its activities; it does not
delete its evidence. Delete sensitive artifacts when the investigation ends,
and apply your own backup and retention rules to copies and exported reports.

## Network capture and protection

Network interception is optional and requires explicit consent. The proxy binds
to localhost. Captured traffic can include hostnames, URLs, headers, timing,
status codes, and body previews. Full-body capture is off by default and can be
enabled explicitly.

Recognized credential headers, URL parameters, and credential-shaped fields are
redacted in network artifacts; the command journal also masks sensitive
arguments. Redaction cannot guarantee removal of every kind of personal or
sensitive information. Screenshots, recordings, logs, and application files can
still contain sensitive data. Use test accounts and sample data, and review
artifacts before sharing them.

## Recipients and external services

Local device tools and the AI assistant you choose may access the evidence as
part of your authorized task. Traffic from the tested app continues to its
intended services unless you mock it. Installing tools downloads software from
the package sources you choose, which receive normal download requests.

If you export, upload, or share evidence, the recipients and storage services
you select receive that data and apply their own retention policies. Autonom
does not sell test data or use it for advertising. Do not expose its local
device-control bridge publicly.

## Your controls and contact

You control the test target, commands, network capture, optional body capture,
exports, and sharing. You can stop capture, detach the proxy, stop sessions, and
delete local artifacts and app knowledge. Consult
[Security](https://github.com/aiatsuk/autonom/blob/main/SECURITY.md) before
intercepting traffic or changing simulator trust settings.

For privacy questions, contact the maintainer through the
[Autonom repository](https://github.com/aiatsuk/autonom). Public support issues
are visible to others and retained by GitHub; do not post personal data,
credentials, or sensitive test artifacts there. Information you voluntarily
send for support is used to address your request and is subject to the hosting
service's retention and deletion controls.
