# Known Issues

Issues currently under review in GB Energy Monitor. These are open, unresolved,
or under investigation — resolved items move to `CHANGELOG.md`.

All items here relate to the project's governing principle: **honesty over
plausibility**. Estimated, derived, stale, or basis-mismatched data must be
labelled as such; a display that is individually accurate but invites a
misleading reading is treated as a bug.

Last reviewed: 260825.

---

## Open

_None currently open._

The solar-resource-verdict elevation issue (the "solar strong" badge reading
strong near dawn/dusk while actual output was ~1%) was resolved in 260825 by
gating `_rate_solar` on computed sun elevation — see `CHANGELOG.md`.

---

## Notes

- The header text on the gas margin card correctly frames the derived signal as
  a proxy, not an official Margins Notice or Gas Balancing Notification.
- Resolved issues are not kept here; see `CHANGELOG.md`.
