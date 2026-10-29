# Snapmaker Orca CLI probe (2026-10-28)

Throwaway scripts used to verify headless slicing on Snapmaker Orca 01.10.01.50.
Findings are summarised in `docs/verified-facts.md` section 2. Not part of the package.

- `cube.stl` — 20 mm test cube.
- `flatten.py <system_dir> <kind> <preset name> <out.json>` — resolves the
  `inherits` chain of a system preset into one JSON file. Sets `"from": "User"`;
  the machine preset then needs `"from": "system"` for the CLI compatibility
  check.
- `bisect.py <work_dir>` — finds which process keys crash the CLI. Expects in
  `<work_dir>`: `p.json` (full flattened process), `pmin.json` (minimal process
  that loads), `m_s.json` (machine with `"from": "system"`), `f.json`
  (filament), `cube.stl`. Found: `wipe_tower_filament`.
