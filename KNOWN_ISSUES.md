# Known Issues

Issues currently under review in GB Energy Monitor. These are open, unresolved,
or under investigation — resolved items move to `CHANGELOG.md`.

All items here relate to the project's governing principle: **honesty over
plausibility**. Estimated, derived, stale, or basis-mismatched data must be
labelled as such; a display that is individually accurate but invites a
misleading reading is treated as a bug.

Last reviewed: 260824.

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

## Notes

- The header text on the gas margin card correctly frames the derived signal as
  a proxy, not an official Margins Notice or Gas Balancing Notification.
- Resolved issues are not kept here; see `CHANGELOG.md`.
