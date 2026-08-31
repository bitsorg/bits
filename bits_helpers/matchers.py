# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Requirement/variable/version matching for recipe `requires:`, defaults
`variables:` gates, patch `when:` entries and version pins. Split out of
utilities.py; pure logic. Imports the arch-derived variables (for `(?osx)`-style
gates) from bits_helpers.arch, so this sits above utilities in the import graph
(utilities imports back the few matcher entry points getPackageList needs)."""

import re
from collections import OrderedDict

from bits_helpers.log import debug, dieOnError
from bits_helpers.arch import predefined_arch_vars

def _parse_req_matcher(r):
  """Split a requirement string into ``(name, matcher, version_pin)`` triple.

  Supported syntaxes::

      name                          plain dependency
      name:matcher                  architecture/defaults-conditional dependency
      name = version                dependency with explicit version pin
      name = version:matcher        version pin + arch/defaults condition

  *matcher* is an architecture regex or ``defaults=<regex>``, exactly as for
  the two-field form.  *version_pin* is ``None`` when no ``= version`` clause
  is present.

  The ``=`` must appear **before** the ``:`` (if any) so that version strings
  containing ``:`` are not ambiguous with matchers.  In practice version
  strings do not contain ``:``, so this is not a real constraint.
  """
  # Locate = and : positions.  Only treat = as a version separator when it
  # appears before the first : (or when there is no :).
  eq_pos = r.find("=")
  colon_pos = r.find(":")
  if eq_pos != -1 and (colon_pos == -1 or eq_pos < colon_pos):
    name = r[:eq_pos].strip()
    rest = r[eq_pos + 1:].strip()
    if ":" in rest:
      pin, matcher = rest.split(":", 1)
      return name, matcher, pin.strip()
    return name, ".*", rest
  if ":" in r:
    name, matcher = r.split(":", 1)
    return name, matcher, None
  return r, ".*", None


def _defaults_active(matcher, defaults):
  """Return True if a ``defaults=<regex>`` *matcher* matches the active defaults.

  ``defaults`` is what bits threads through from ``args.defaults``, which is a
  *list* of profile names (``--defaults dev4::cuda`` -> ``["dev4", "cuda"]``);
  older callers/tests may pass a bare string.  The conditional is active when the
  regex matches ANY active profile, so a recipe can require a dependency only
  under a given profile, e.g. ``- "cuda:defaults=cuda"`` (enabled by
  defaults-cuda.sh).  Matching per-element also makes this safe: the previous
  code passed the whole list to ``re.match`` and would raise TypeError.
  """
  rx = matcher[len("defaults="):]
  defs = defaults if isinstance(defaults, (list, tuple)) else [defaults]
  return any(re.match(rx, d) for d in defs)


# A variable-reference matcher is spelled "(?NAME)" -- an identifier in the same
# parenthesised form as a regex group, but one that is NOT a legal regex (e.g.
# "(?cuda)" raises re.error: "unknown extension ?c").  This lets a recipe gate a
# dependency on a defaults *variable* rather than on the architecture string:
#   - "cuda:(?cuda)"      # require cuda only when variable `cuda` is truthy
# It is deliberately distinct from arch regexes such as "(?!osx)" (a valid
# negative-lookahead, kept as an arch match) -- we only treat "(?NAME)" as a
# variable reference when it fails to compile as a regex, so real regexes
# (including inline-flag groups like "(?i)") are never misinterpreted.
_VAR_MATCHER_RE = re.compile(r"\(\?([A-Za-z_][A-Za-z0-9_]*)\)\Z")


def _var_matcher_name(matcher):
  """Return the variable NAME if *matcher* is a "(?NAME)" variable reference,
  else None (in which case it is an arch regex / defaults= matcher)."""
  m = _VAR_MATCHER_RE.match(matcher or "")
  if not m:
    return None
  try:
    re.compile(matcher)
  except re.error:
    return m.group(1)   # not a valid regex -> it's a variable reference
  return None            # valid regex (e.g. "(?i)") -> treat as arch match


def _var_truthy(default_vars, name):
  """True when defaults variable *name* is defined and not a false-ish string."""
  v = (default_vars or {}).get(name)
  return v is not None and str(v).strip().lower() not in ("", "0", "false", "off", "no")


def _loose_version_key(v):
  """A natural-order sort key for version strings, à la ``sort -V``.

  Splits the string into runs of digits and non-digits; digit runs compare
  numerically (so v40r2 < v40r10) and non-digit runs lexicographically. Each
  element is a (type, value) tuple so int and str runs never compare directly.
  Handles the schemes bits sees: v40r2, v01-19-06, 01.07, 1.2.3, 0.1.0pre17.

  Separator characters ``-``, ``.`` and ``_`` are treated as equivalent and do
  not themselves contribute to the ordering, so dash- and dot-form tags compare
  equal (``v6-40-00`` == ``v6.40.00``). Without this, the raw separator runs
  sort lexicographically ('-' 0x2d < '.' 0x2e), which made ``v6-40-00`` rank
  below ``v6.36.99`` and silently broke ``version>=`` gating for ROOT-style
  dash tags.
  """
  key = []
  for p in re.findall(r"\d+|\D+", str(v)):
    if p.isdigit():
      key.append((0, int(p)))
    else:
      s = re.sub(r"[-._]+", "", p)   # drop separators; keep alpha (v, r, pre…)
      if s:
        key.append((1, s))
  return key


def _version_compare(a, b):
  """Return -1/0/1 comparing version strings *a* and *b* in natural order."""
  ka, kb = _loose_version_key(a), _loose_version_key(b)
  return (ka > kb) - (ka < kb)


# version<op><value>: e.g. "version=v40r2", "version<v40r4", "version>=v40r2".
_VERSION_OP_RE = re.compile(r"version\s*(>=|<=|==|!=|=|>|<)\s*(.+)\Z", re.DOTALL)
_VERSION_OPS = {
    "=":  lambda c: c == 0, "==": lambda c: c == 0, "!=": lambda c: c != 0,
    "<":  lambda c: c < 0,  "<=": lambda c: c <= 0,
    ">":  lambda c: c > 0,  ">=": lambda c: c >= 0,
}


def _matcher_atom_active(matcher, arch, defaults, default_vars=None, version=None):
  """Evaluate a single (non-compound) matcher atom. See _matcher_active."""
  if matcher.startswith("defaults="):
    return _defaults_active(matcher, defaults)
  vm = _VERSION_OP_RE.match(matcher)
  if vm:
    return version is not None and _VERSION_OPS[vm.group(1)](_version_compare(version, vm.group(2).strip()))
  var = _var_matcher_name(matcher)
  if var is not None:
    return _var_truthy(default_vars, var)
  return bool(re.match(matcher, arch))


def _matcher_active(matcher, arch, defaults, default_vars=None, version=None):
  """Whether a *matcher* is active for the current build.

  Atoms:
    * ``defaults=<regex>``       -> active when the regex matches an active profile;
    * ``version<op><value>``     -> active when the package version satisfies the
                                    comparison (op is one of = == != < <= > >=),
                                    e.g. ``foo.patch:version=v40r2`` or
                                    ``foo.patch:version<v40r4``. Versions compare
                                    in natural order (sort -V semantics);
    * ``(?VAR)``                 -> active when defaults variable VAR is truthy;
    * anything else              -> a regex matched against the architecture string.

  Atoms may be combined with ``&&`` (all) and ``||`` (any); ``||`` has the lower
  precedence, e.g. ``(?!osx) && version>=v40r2 || (?cuda)`` is
  ``((?!osx) AND version>=v40r2) OR (?cuda)``. (Note: a single ``|`` inside an
  arch regex is still ordinary alternation — only the doubled ``||`` combines.)

  *version* is the resolved package version (after overrides / pins); it is only
  consulted by the ``version`` kind and may be ``None`` for callers that never
  use it (e.g. requires filtering).
  """
  matcher = matcher.strip()
  if "||" in matcher:
    parts = [p for p in (s.strip() for s in matcher.split("||")) if p]
    return any(_matcher_active(p, arch, defaults, default_vars, version) for p in parts)
  if "&&" in matcher:
    parts = [p for p in (s.strip() for s in matcher.split("&&")) if p]
    return all(_matcher_active(p, arch, defaults, default_vars, version) for p in parts)
  return _matcher_atom_active(matcher, arch, defaults, default_vars, version)




def resolve_variables(variables, flavours, architecture, defaults):
  """Resolve a defaults ``variables:`` block into a flat ``{name: value}`` dict.

  Entries may be plain (``name: value`` -- always defined) or *gated*
  (``name: {value: V, when: MATCHER}`` -- defined to ``V`` only when ``MATCHER``
  is active for this build). ``MATCHER`` uses the requires-matcher grammar
  (``(?flavour)``, an architecture regex such as ``osx`` / ``(?!osx)``,
  ``defaults=<regex>``, combined with ``&&`` / ``||``) and is evaluated against
  the variables resolved *so far*, so a gate may reference CLI flavours, the
  predefined architecture variables, and any earlier entry ("a previously
  defined variable"). A gated entry with no explicit ``value`` defaults to
  ``True`` when active.

  Precedence (low -> high): predefined arch vars < CLI flavours < defaults-file
  entries, except that a CLI flavour always wins over a defaults entry of the
  same name (an explicit override) while remaining visible to every gate.
  """
  flavours = flavours or {}
  resolved = OrderedDict()
  resolved.update(predefined_arch_vars(architecture))
  resolved.update(flavours)                        # visible to the gates below
  for name, entry in (variables or {}).items():
    if name in flavours:
      continue                                     # CLI flavour overrides defaults
    if isinstance(entry, dict) and "when" in entry:
      if _matcher_active(str(entry["when"]), architecture, defaults, resolved):
        resolved[name] = entry.get("value", True)
      # inactive -> leave undefined (falsy)
    else:
      resolved[name] = entry
  return resolved


def filterByArchitectureDefaults(arch, defaults, requires, default_vars=None, version=None):
  """Yield requirements from *requires* that are satisfied by *arch*/*defaults*.

  *version* is the depending package's own resolved version; pass it so a
  requirement can be gated on it, e.g. ``- "curl:version>=v6.40.00"``.
  """
  for r in requires:
    require, matcher, _pin = _parse_req_matcher(r)
    if _matcher_active(matcher, arch, defaults, default_vars, version):
      yield require

def disabledByArchitectureDefaults(arch, defaults, requires, default_vars=None, version=None):
  """Yield requirements from *requires* that are *not* satisfied by *arch*/*defaults*."""
  for r in requires:
    require, matcher, _pin = _parse_req_matcher(r)
    if not _matcher_active(matcher, arch, defaults, default_vars, version):
      yield require


def _parse_patch_entry(entry):
  """Split a ``patches:`` entry into ``(name, matcher_or_None, checksum_suffix)``.

  Entry form: ``name[:matcher][,algo:digest]``. The optional inline checksum
  (which itself contains ``:``) is separated first on the first ``,``; a ``:``
  in the remaining head then introduces a conditional matcher, e.g.
  ``foo.patch:version<v40r4`` or ``foo.patch:(?cuda),sha256:abc``.
  """
  head, sep, tail = entry.partition(",")
  checksum = (sep + tail) if sep else ""
  name, csep, matcher = head.partition(":")
  return name.strip(), (matcher.strip() if csep else None), checksum


def filterPatches(patches, arch, defaults, default_vars, version):
  """Return the ``patches:`` entries active for this build, with any ``:matcher``
  stripped so downstream (checksum lookup, copy to $SOURCEDIR, ``patch``) sees a
  plain ``name[,algo:digest]``. Entries without a matcher are always kept."""
  out = []
  for entry in patches or []:
    name, matcher, checksum = _parse_patch_entry(entry)
    if matcher is None or _matcher_active(matcher, arch, defaults, default_vars, version=version):
      out.append(name + checksum)
  return out


def _collect_version_pins(arch, defaults, raw_requires, owner, version_pins, specs,
                          default_vars=None, version=None):
  """Extract version pins from *raw_requires* and merge into *version_pins*.

  Called while processing *owner*'s spec (before the requires list has been
  reduced to plain names).  Any ``name = version`` clause that is active for
  the current *arch*/*defaults* pair is registered in *version_pins*.

  Raises a :exc:`SystemExit` (via :func:`dieOnError`) when:

  * Two different packages pin the same dependency to **different** versions.
  * A version pin is declared for a dependency that was already resolved with
    a different version (i.e. ``name in specs`` with a conflicting version).
    This happens when the pinned package appeared in the build queue before the
    package that declares the pin, making the pin arrive too late.
  """
  for r in raw_requires:
    name, matcher, pin = _parse_req_matcher(r)
    if pin is None:
      continue
    # Check whether this entry is active for the current architecture/defaults.
    if not _matcher_active(matcher, arch, defaults, default_vars, version):
      continue
    if name in version_pins:
      if version_pins[name] != pin:
        dieOnError(True,
          "Conflicting version pin for '%s': '%s' (from an earlier spec) vs "
          "'%s' (from '%s'). Only one version pin per dependency is allowed."
          % (name, version_pins[name], pin, owner))
      # Same pin value from multiple packages — harmless, nothing to do.
      continue
    if name in specs:
      actual = specs[name].get("version", "")
      if actual != pin:
        dieOnError(True,
          "Version pin '%s = %s' declared by '%s' cannot be applied: '%s' was "
          "already resolved with version '%s'. Move the pinning package earlier "
          "in the build list, or remove the conflicting pin."
          % (name, pin, owner, name, actual))
      # Already resolved with the same version — no action needed.
      continue
    version_pins[name] = pin
    debug("Version pin registered: %s = %s  (from %s)", name, pin, owner)
