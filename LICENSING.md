# Licensing

The bits ecosystem spans several repositories under two licenses, chosen by
provenance rather than preference.

## Why two licenses

`bits` and its recipe repositories descend from ALICE's **aliBuild** and
**alidist**, both licensed under **GPL-3.0**. Under the GPL's copyleft, these
derivative works must remain GPL-3.0-or-later.

The newer services written from scratch for the CVMFS publish chain — the Go
publisher (`cvmfs-bits` / cvmfs-prepub) and its deployment example
(`cvmfs-testbed`) — contain no aliBuild code, so they use the permissive
**Apache-2.0** license. Apache-2.0 is one-way compatible *into* GPL-licensed
combinations, so these components can still be combined with the GPL parts.

## Per-component licenses

| Component | License | SPDX identifier | Provenance |
|-----------|---------|-----------------|------------|
| `bits` (core) | GPL-3.0-or-later | `GPL-3.0-or-later` | derived from aliBuild |
| `common.bits`, `lcg.bits` | GPL-3.0-or-later | `GPL-3.0-or-later` | recipes, derived from alidist |
| `bits-recipe-tools` | GPL-3.0-or-later | `GPL-3.0-or-later` | recipe helper snippets |
| `bits-console` | GPL-3.0-or-later | `GPL-3.0-or-later` | web UI |
| `cvmfs-bits` (cvmfs-prepub) | Apache-2.0 | `Apache-2.0` | new Go service |
| `cvmfs-testbed` | Apache-2.0 | `Apache-2.0` | deployment example |

Repositories that do not yet carry a `LICENSE` file (`bits-providers`,
`stacks.bits`, `remote-runner`) should adopt **GPL-3.0-or-later** to match the
recipe/core tooling.

Each licensed source file carries an `SPDX-License-Identifier` header. The
`bits-recipe-tools` snippets are a deliberate exception: they are hashed by
bits for content addressing, so per-file headers are omitted there to keep
build hashes stable — that repository's `LICENSE` and `COPYRIGHT` govern all
its files.

## Copyright & contributions

Copyright (C) CERN (European Organization for Nuclear Research) and the bits
project contributors. Work produced by CERN personnel is owned by CERN; please
involve CERN Knowledge Transfer before changing any license.

Contributions are accepted under the **Developer Certificate of Origin (DCO)**:
sign off your commits with `git commit -s`.

