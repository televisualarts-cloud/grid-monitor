# Known Issues

Issues currently under review in GB Energy Monitor. These are open, unresolved,
or under investigation — resolved items move to `CHANGELOG.md`.

All items here relate to the project's governing principle: **honesty over
plausibility**. Estimated, derived, stale, or basis-mismatched data must be
labelled as such; a display that is individually accurate but invites a
misleading reading is treated as a bug.

Last reviewed: 260823.

---

## Open

### Solar resource verdict ignores solar elevation
**Component:** EA page — Resource Conditions rollup (`grid_server.py`)
**Status:** confirmed, fix not yet implemented

The "solar strong N/N good" rollup badge is computed from cloud/output
conditions at the probed solar sites and does not account for solar elevation.
Near sunset a site can read "clear — near full output" on cloud terms while the
sun is too low to produce meaningful irradiance, so the badge can show "strong"
while actual national solar output is ~1% of supply.

Both figures are individually honest (the badge is a sky-condition verdict; the
output figure is measured, tagged EST/PVLive), but "strong" next to near-zero
output reads as a claim about output that it is not. Individual site cards
already handle this — night sites show "night — no solar output" and drop from
the good count; the gap is only in the aggregate rollup.

Candidate fixes:
- Gate the solar rollup by a solar-elevation floor: below a threshold (e.g. sun
  < 5–10°), downgrade or suppress the "strong/good" wording, mirroring how night
  sites already drop out. (Preferred — most in keeping with the principle.)
- Or append an elevation qualifier to the badge so "strong" cannot stand alone
  near dusk.

---

### Gas balance trend: linepack can rise while supply−demand shows drawdown
**Component:** Gas page — Balance Trend plot (`grid_server.py`, `get_gas`)
**Status:** under investigation — diagnostic in place

On the 48h balance trend, the linepack line (cyan) sometimes rises at the same
instant the supply−demand line (amber) is negative. A viewer naturally reads
amber as the derivative of cyan (negative balance ⇒ falling linepack), but the
two series are not a matched derivative pair, so the relationship does not hold
instant-by-instant.

Two candidate mechanisms, not yet distinguished:
1. **Intra-point timestamp skew.** Linepack is captured every ~2 min but
   republished by National Gas every ~12 min, while supply/demand flows update on
   their own cadence. A single logged point can pair a stale linepack with
   fresher flows. The point is keyed on `linepack_at`, but the flow timestamps
   were previously discarded, so skew was invisible.
2. **Archive→live splice basis mismatch.** The plot fills earlier hours from the
   Published Data API hourly-actual archive and uses the local live log for
   current hours. The dedup is on an hour bucket, so at the boundary the settled
   hourly-actual linepack meets the raw live spot linepack — two different
   measurement bases — and the cyan line can step for reasons unrelated to the
   concurrent flow balance.

**Diagnostic:** a temporary append-only probe in `get_gas` writes one NDJSON row
per poll to `gas_skew_diag.ndjson`, recording `linepack_at`, `supply_total_at`,
`demand_total_at`, `published`, and their pairwise skews in seconds. Interpretation:
- Non-zero `skew_supply_minus_linepack_s` / `skew_demand_minus_linepack_s` with
  near-zero `skew_supply_minus_demand_s` ⇒ mechanism 1 (intra-point skew).
- All skews near zero ⇒ mechanism 2 (splice basis mismatch); investigate the
  hour-bucket dedup.

Diagnostic is throwaway (no build tag bumped); remove the `DIAG (temporary)`
block and the two `_at` capture lines once the mechanism is confirmed.

Candidate fixes (pending diagnosis):
- If skew: stamp each point's linepack and balance with their own source
  timestamps and only plot balance where its timestamp is within tolerance of
  the linepack timestamp.
- If splice: mark or style the unsettled leading live segment distinctly so it is
  not read as reconciled against the archive.

The header signal itself ("Gas margin tight" / derived early-warning proxy)
already hedges this correctly and is not affected; only the trend plot is.

---

## Notes

- The header text on the gas margin card correctly frames the derived signal as
  a proxy, not an official Margins Notice or Gas Balancing Notification.
- Resolved issues are not kept here; see `CHANGELOG.md`.
