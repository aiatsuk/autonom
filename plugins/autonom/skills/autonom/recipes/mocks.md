# Recipe: see requests, mock a response, wait for a request

Capture decrypts the session's traffic and changes the device's proxy. It is
consent-gated: pass `--i-understand-mitm` on every `start` and `attach`, only
for apps and devices you may test.

```bash
autonom network start --i-understand-mitm          # a proxy on 127.0.0.1
autonom network attach --i-understand-mitm         # point the device at it
autonom network status                             # running? attached?
```

## See what the app sent

```bash
autonom ui tap --text "Refresh" --until 'request=*/v1/orders*'   # act and wait for the call
autonom network requests list --path '*/v1/orders*' --max 5
autonom network requests show f_0003               # one request, redacted previews
```

The wait works for any app whose traffic the proxy sees: no app hooks needed.
On its own: `autonom network requests wait --path '*/v1/orders*' --status 200`.

## Force a response

```bash
# answer instead of the server
autonom network mock add --match '*/v1/orders*' --method GET --status 500 --json '{"error":"x"}'
# or let the request through and edit the real answer
autonom network mock add --match '*/v1/orders*' --rewrite --json-set 'items.0.status="cancelled"'
# a slow backend: the real answer, 8 s late
autonom network mock add --match '*/v1/orders*' --rewrite --delay 8

autonom ui tap --text "Refresh" --until 'request=*/v1/orders*' --until text="Something went wrong" --shot "orders 500"
autonom network mock list                          # ids and hit counts
```

Narrow a rule with `--query NAME=GLOB`, `--request-header 'NAME: GLOB'`,
`--body-contains TEXT`, `--body-json PATH=VALUE`. Mocks are machine-wide and
stay after the session: switch them off when done.

## Clean up

```bash
autonom network mock disable --all                 # or: mock remove <id>
autonom network stop                               # detaches the device first
```

## Rules

- A screenshot taken while a mock is active shows mocked data
  (`screenshot_shows_mocked_data`). Never present it as real backend behaviour.
- `proxy_not_running`: start and attach first. `--until request=` refuses
  before it sends any input.
- A request that never shows up: `autonom network status` (is the device
  attached?), then `autonom network requests list --max 10` (is anything
  recorded? is the filter right?).

More: the `mobile-network` skill (trust store, bodies, HAR export, cleanup
failures).
