# CycloneDX Sunshine (vendored)

`sunshine.py` and `LICENSE` are copied unmodified from
https://github.com/CycloneDX/Sunshine at commit
`beaab3faf2bab234069a8677fc627de2f0f01a3d` (Apache-2.0, OWASP Foundation).

The backend runs it as a script to turn a build's CycloneDX SBOM into an HTML
report (`GET /ops/builds/{pipeline}/sbom/report`, `orch/sbom_report.py`). It is
never run with `-e`, which would fetch EPSS and CISA KEV data from a third-party
site.

To update, copy both files from a newer commit, change the commit above, and run
`tests/test_orch_sbom_report.py`.
