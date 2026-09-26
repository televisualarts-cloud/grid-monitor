# Known Issues

Issues currently under review in GB Energy Monitor. These are open, unresolved,
or under investigation — resolved items move to `CHANGELOG.md`.

All items here relate to the project's governing principle: **honesty over
plausibility**. Estimated, derived, stale, or basis-mismatched data must be
labelled as such; a display that is individually accurate but invites a
misleading reading is treated as a bug.

Last reviewed: 260913.

---

## Open

- Weather and EA rain gauge API feeds are flakey. EA endpoints regularly error 503;
  OM and OWM APIs appear to time out. Using a fixed IP and avoiding a VPN doesn't
  seem to help. Investigations ongoing (see the candidate issues below).

---

## Candidate issues — EA feed robustness (UNCONFIRMED; confirm by analysis before any change)

Reported 260913 after an EA flood-monitoring API stall. These are **hypotheses to
investigate, not accepted diagnoses** — each must be confirmed against the code and a
reproduced stall before any change is made. Where evidence already on hand bears on a
claim it is noted; honesty over plausibility applies to this list too.

### C1. EA "latest readings" payload size — `/data/readings?latest&_limit=15000`
- **Hypothesis:** the latest-index fetch (`_ea_latest_index()`, grid_server.py ~L4638,
  `timeout=60`) asks EA to assemble ~15,000 records into one JSON blob; on a degraded EA
  gateway this stalls with the TCP connection held open until the client times out.
- **Proposed fix:** switch to `/id/measures?_limit=1000` (or parameter-filtered queries)
  and read each value from the nested `latestReading` object instead of top-level `items`.
- **Status:** UNCONFIRMED. The `_limit=15000` request is real and worth trimming for
  routine robustness/bandwidth. BUT on 260911–13 even a `_limit=1` EA query timed out
  (tested from two independent IPs including a Linux/OpenSSL host), so payload size did
  **not** cause that outage — it was an EA-side total stall. Treat payload trimming as a
  chronic-slowness improvement, not the cure for a full outage. Before switching, confirm
  the `measures[]`/`latestReading` schema still yields every gauge the dashboard needs.

### C2. Windows Schannel OCSP revocation freeze
- **Hypothesis:** urllib on Windows uses native Schannel, which does a background OCSP
  revocation check on Azure-hosted `.gov.uk` certs; if that hangs, `urlopen` freezes for
  30–60 s, ignoring the socket timeout.
- **Proposed fix:** force OpenSSL, or shell out to `curl --ssl-no-revoke`.
- **Status:** UNCONFIRMED — and likely a misdiagnosis. Standard CPython's `ssl`/`urllib`
  use bundled **OpenSSL, not Windows Schannel**, so the OCSP-via-Schannel mechanism does
  not apply to a normal Python build; and the same stall reproduced on a Linux/OpenSSL
  host today, which points to an EA-server-side hang rather than a local TLS/OCSP issue.
  Confirm the actual backend (`ssl.OPENSSL_VERSION`) before spending effort here. The real
  robustness lever is a hard, socket-honoured per-request timeout (see C4), not the TLS layer.

### C3. No gzip transport negotiation
- **Hypothesis:** `fetch_json` does not send `Accept-Encoding: gzip, deflate`, so EA
  returns uncompressed payloads — slower transfers and more drops on a poor link.
- **Proposed fix:** send `Accept-Encoding: gzip, deflate` and `gzip.decompress()` the body
  when the response is gzip-encoded.
- **Status:** UNCONFIRMED but plausible and low-risk; a genuine efficiency win for the
  large latest-index payload (pairs well with C1). Confirm `fetch_json`'s current headers
  and add transparent gzip handling. Not a cause of a full stall.

### C4. EA fetch blocking / fail-fast caching
- **Hypothesis:** a synchronous EA fetch in the request path freezes the whole app when EA
  hangs, blocking other routines and endpoints.
- **Proposed fix:** fail-fast worker/background pattern; on `URLError`/`TimeoutError`
  return the cached index (`_ea_latest_cache["idx"]`) with `stale=True` instead of blocking.
- **Status:** PARTLY MITIGATED — confirm scope. The server is already `ThreadingHTTPServer`
  (L63 / L7102), so a hung EA fetch blocks only that request's own thread, not the whole
  app or the `/api/grid` path — the "freezes everything" impact is overstated.
  `_ea_latest_cache` already exists with `idx`/`stale` fields (L4537). Real work to confirm:
  (a) does `_ea_latest_index()` on timeout actually return the cached `idx` with
  `stale=True`, or propagate the exception and blank the panel? (b) is the effective wait
  bounded — the 60 s `timeout` is long; a shorter fail-fast timeout + serve-stale is the
  main win; (c) optionally move the index refresh to a background sampler like the
  powercuts one.

---

## Notes

- Resolved issues are not kept here; see `CHANGELOG.md`.
