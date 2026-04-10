import configparser
from bits_helpers.git import git, Git
from bits_helpers.utilities import getPackageList, parseDefaults, readDefaults, validateDefaults
from bits_helpers.log import debug, error, warning, banner, info
from bits_helpers.log import dieOnError
from bits_helpers.workarea import updateReferenceRepoSpec
from bits_helpers.cmd import getstatusoutput
from io import StringIO

from os.path import join
import os.path as path
import os
import sys


def parsePackagesDefinition(pkgname):
  return [ dict(zip(["name","ver"], y.split("@")[0:2]))
           for y in [ x+"@" for x in list(filter(lambda y: y, pkgname.split(","))) ] ]


# Mapping: (argparse attribute, bits.rc key, short-flag alias or None)
# Used by doInitConfig to decide which settings to persist.
_INIT_RC_MAP = [
    # attr              rc_key               short_flag
    ("providers",       "providers",          None),
    ("initRemoteStore", "remote_store",       None),
    ("initWriteStore",  "write_store",        None),
    ("organisation",    "organisation",       None),
    ("workDir",         "work_dir",           "w"),
    ("architecture",    "architecture",       "a"),
    ("defaults",        "defaults",           None),
    ("configDir",       "config_dir",         "c"),
    ("referenceSources","reference_sources",  None),
]

# Canonical flag names that map directly to an rc key (long-form, normalised).
_LONG_FLAG_TO_RC = {entry[0].lower(): entry[1] for entry in _INIT_RC_MAP}
_LONG_FLAG_TO_RC.update({
    "remote_store":       "remote_store",
    "write_store":        "write_store",
    "work_dir":           "work_dir",
    "config_dir":         "config_dir",
    "reference_sources":  "reference_sources",
})
_SHORT_FLAG_TO_ATTR = {entry[2]: entry[0] for entry in _INIT_RC_MAP if entry[2]}


def _explicit_rc_keys(explicit_flags):
    """Return the set of bits.rc keys the user explicitly requested."""
    keys = set()
    for flag in explicit_flags:
        if flag in _LONG_FLAG_TO_RC:
            keys.add(_LONG_FLAG_TO_RC[flag])
        elif flag in _SHORT_FLAG_TO_ATTR:
            attr = _SHORT_FLAG_TO_ATTR[flag]
            for a, rc_key, _ in _INIT_RC_MAP:
                if a == attr:
                    keys.add(rc_key)
                    break
    return keys


def doInitConfig(args):
    """Write (or update) a bits.rc from the options supplied on the CLI.

    Only settings that the user explicitly named on the command line are
    written; default values for options the user did not mention are skipped
    so that bits.rc stays minimal and authoritative.

    With --dry-run the resulting INI content is printed without touching the
    file system.
    """
    rc_file   = getattr(args, "rcFile",    "bits.rc")
    append    = getattr(args, "appendRc",  False)
    explicit  = getattr(args, "_init_explicit", set())

    # Which bits.rc keys did the user explicitly request?
    rc_keys_to_write = _explicit_rc_keys(explicit)

    if not rc_keys_to_write:
        info("No configuration options specified — nothing to write.\n"
             "Run 'bits init --help' to see available persistent settings.\n"
             "To clone package sources for development, supply a PACKAGE name:\n"
             "  bits init [--dist USER/REPO@BRANCH] PACKAGE")
        return

    cfg = configparser.ConfigParser()
    if append and path.exists(rc_file):
        cfg.read(rc_file)
        debug("Merging into existing %s", rc_file)
    if not cfg.has_section("bits"):
        cfg.add_section("bits")

    for attr, rc_key, _short in _INIT_RC_MAP:
        if rc_key not in rc_keys_to_write:
            continue
        val = getattr(args, attr, None)
        if val is None:
            continue
        # args.defaults is already split into a list by finaliseArgs
        if isinstance(val, list):
            val = "::".join(val)
        cfg.set("bits", rc_key, str(val))
        debug("bits.rc: %s = %s", rc_key, val)

    buf = StringIO()
    cfg.write(buf)
    ini_text = buf.getvalue()

    if args.dryRun:
        info("Would write to %s:\n\n%s", rc_file, ini_text)
        return

    with open(rc_file, "w") as fh:
        fh.write(ini_text)
    banner("Configuration written to %s", rc_file)


def doInit(args):
  assert(args.pkgname != None)

  pkgs = parsePackagesDefinition(args.pkgname) if args.pkgname else []

  # ── Config mode ────────────────────────────────────────────────────────────
  # When no PACKAGE is given, treat the invocation as a request to write (or
  # update) bits.rc from the supplied options.  This is backward-compatible:
  # old callers that supply a PACKAGE are unaffected.
  if not pkgs:
    return doInitConfig(args)

  # ── Clone mode (existing behaviour) ────────────────────────────────────────
  assert(type(args.dist) == dict)
  assert(sorted(args.dist.keys()) == ["repo", "ver"])
  assert(type(pkgs) == list)

  if args.dryRun:
    info("This will initialise local checkouts for %s\n"
         "--dry-run / -n specified. Doing nothing.", ",".join(x["name"] for x in pkgs))
    sys.exit(0)
  try:
    path.exists(args.develPrefix) or os.mkdir(args.develPrefix)
    path.exists(args.referenceSources) or os.makedirs(args.referenceSources)
  except OSError as e:
    error("%s", e)
    sys.exit(1)

  # Fetch recipes first if necessary
  if path.exists(args.configDir):
    warning("using existing recipes from %s", args.configDir)
  else:
    cmd = ["clone", "--origin", "upstream",
           args.dist["repo"] if ":" in args.dist["repo"] else "https://github.com/" + args.dist["repo"]]
    if args.dist["ver"]:
      cmd.extend(["-b", args.dist["ver"]])
    cmd.append(args.configDir)
    git(cmd)

  # Use standard functions supporting overrides and taps. Ignore all disables
  # and system packages as they are irrelevant in this context
  specs = {}
  defaultsReader = lambda: readDefaults(args.configDir, args.defaults, lambda msg: error("%s", msg), args.architecture)
  (err, overrides, taps, _defaultsMeta) = parseDefaults([], defaultsReader, debug)
  (_,_,_,validDefaults) = getPackageList(packages=[ p["name"] for p in pkgs ],
                                         specs=specs,
                                         configDir=args.configDir,
                                         preferSystem=False,
                                         noSystem="*",
                                         architecture="",
                                         disable=[],
                                         defaults=args.defaults,
                                         performPreferCheck=lambda pkg, cmd: getstatusoutput(["bash", "-c", cmd]),
                                         performRequirementCheck=lambda *x, **y: (0, ""),
                                         performValidateDefaults=lambda spec : validateDefaults(spec, args.defaults),
                                         overrides=overrides,
                                         taps=taps,
                                         log=debug)
  dieOnError(validDefaults and any(d not in validDefaults for d in args.defaults),
             "Specified default `%s' is not compatible with the packages you want to build.\n" % "::".join(args.defaults) +
             "Valid defaults:\n\n- " +
             "\n- ".join(sorted(validDefaults)))

  for p in pkgs:
    spec = specs.get(p["name"])
    spec["is_devel_pkg"] = False
    spec["scm"] = Git()
    dieOnError(spec is None, "cannot find recipe for package %s" % p["name"])
    dest = join(args.develPrefix, spec["package"])
    writeRepo = spec.get("write_repo", spec.get("source"))
    dieOnError(not writeRepo, "package %s has no source field and cannot be developed" % spec["package"])
    if path.exists(dest):
      warning("not cloning %s since it already exists", spec["package"])
      continue
    p["ver"] = p["ver"] if p["ver"] else spec.get("tag", spec["version"])
    debug("cloning %s%s for development", spec["package"], " version "+p["ver"] if p["ver"] else "")

    updateReferenceRepoSpec(args.referenceSources, spec["package"], spec, True, False)

    cmd = ["clone", "--origin", "upstream", spec["source"],
           "--reference", join(args.referenceSources, spec["package"].lower())]
    if p["ver"]:
      cmd.extend(["-b", p["ver"]])
    cmd.append(dest)
    git(cmd)
    git(("remote", "set-url", "--push", "upstream", writeRepo), directory=dest)

    # Make it point relatively to the mirrors for relocation: as per Git specifics, the path has to
    # be relative to the repository's `.git` directory. Don't do it if no common path is found
    repoObjects = os.path.join(os.path.realpath(dest), ".git", "objects")
    refObjects = os.path.join(os.path.realpath(args.referenceSources),
                              spec["package"].lower(), "objects")
    repoAltConf = os.path.join(repoObjects, "info", "alternates")
    if len(os.path.commonprefix([repoObjects, refObjects])) > 1:
      with open(repoAltConf, "w") as fil:
        fil.write(os.path.relpath(refObjects, repoObjects) + "\n")

  banner("Development directory %s created%s", args.develPrefix,
         " for "+", ".join(x["name"].lower() for x in pkgs) if pkgs else "")
