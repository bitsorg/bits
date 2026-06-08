#!/usr/bin/env python3
# Standard library
from os import remove, path
from tempfile import NamedTemporaryFile

# Internal
from bits_helpers.cmd import DockerRunner, execute, getstatusoutput
from bits_helpers.log import debug, dieOnError, error, info
from bits_helpers.utilities import getPackageList, parseDefaults, readDefaults, validateDefaults, resolve_variables

def doDeps(args, parser):

  # Check if we have an output file
  if not args.outgraph:
    parser.error("Specify a PDF output file with --outgraph")

  # Resolve all the package parsing boilerplate
  specs = {}

  def defaultsReader():
    # `bits deps` has no --flavour input, but still resolve `variables:` so any
    # gated entries (and the predefined arch vars) are materialised into a flat
    # map for the (?NAME) matchers rather than leaking raw {value, when} dicts.
    meta, body = readDefaults(args.configDir, args.defaults, parser.error, args.architecture)
    meta["variables"] = resolve_variables(meta.get("variables"), {}, args.architecture, args.defaults)
    return meta, body
  (err, overrides, taps, defaultsMeta) = parseDefaults(args.disable, defaultsReader, debug)
 
  def performCheck(pkg, cmd):
    return getstatusoutput(cmd)
    
  systemPackages, ownPackages, failed, validDefaults = \
      getPackageList(packages                = [args.package],
                     specs                   = specs,
                     configDir               = args.configDir,
                     preferSystem            = args.preferSystem,
                     noSystem                = args.noSystem,
                     architecture            = args.architecture,
                     disable                 = args.disable,
                     defaults                = args.defaults,
                     performPreferCheck      = performCheck,
                     performRequirementCheck = performCheck,
                     performValidateDefaults = lambda spec: validateDefaults(spec, args.defaults),
                     overrides               = overrides,
                     taps                    = taps,
                     log                     = debug,
                     defaults_meta           = defaultsMeta)
  
  dieOnError(validDefaults and any(d not in validDefaults for d in args.defaults),
             "Specified default `%s' is not compatible with the packages you want to build.\n" % "::".join(args.defaults) +
             "Valid defaults:\n\n- " +
             "\n- ".join(sorted(validDefaults)))

  for s in specs.values():
    # Remove disabled packages
    s["requires"] = [r for r in s["requires"] if r not in args.disable and r != "defaults-release"]
    s["build_requires"] = [r for r in s["build_requires"] if r not in args.disable and r != "defaults-release"]
    s["runtime_requires"] = [r for r in s["runtime_requires"] if r not in args.disable and r != "defaults-release"]

  # Determine which packages are only build/runtime dependencies
  all_build   = set()
  all_runtime = set()
  for k,spec in specs.items():
    all_build.update(spec["build_requires"])
    all_runtime.update(spec["runtime_requires"])
  all_both = all_build.intersection(all_runtime)

  dot = "digraph {\n"
  dot += "ratio=\"0.52\"\n"
  dot += 'graph [nodesep=0.25, ranksep=0.2];\n'
  dot += 'node [width=1.5, height=1, fonsize=46, margin=0.1];\n'
  dot += 'edge [penwidth=2];\n'

  for k,spec in specs.items():
    if k == "defaults-release":
      continue

    # Determine node color based on its dependency status
    color = None
    if k in all_both:
      color = "tomato1"
    elif k in all_runtime:
      color = "greenyellow"
    elif k in all_build:
      color = "plum"
    elif k == args.package:
      color = "gold"
    else:
      # A package that is not in any dependency set and is not the top-level
      # target — this should never happen given the getPackageList results.
      raise AssertionError("Unclassified package %r — this is a bug" % k)

    # Node definition
    dot += '"{}" [shape=box, style="rounded,filled", fontname="helvetica", fillcolor={}]\n'.format(k,color)

    # Connections (different whether it's a build dependency or a runtime one)
    for dep in spec["build_requires"]:
     dot += '"{}" -> "{}" [color=grey70]\n'.format(k, dep)
    for dep in spec["runtime_requires"]:
     dot += '"{}" -> "{}" [color=dodgerblue3]\n'.format(k, dep)

  dot += "}\n"

  # Write the DOT source to either the user-supplied path or a temp file.
  if args.outdot:
    dot_path = args.outdot
    with open(dot_path, "w") as fp:
      fp.write(dot)
  else:
    with NamedTemporaryFile(delete=False, mode="wt", suffix=".dot") as fp:
      fp.write(dot)
      dot_path = fp.name

  # Check if we have dot in PATH
  try:
    execute(["dot", "-V"])
  except Exception:
    dieOnError(True, "Could not find dot in PATH. Please install graphviz and add it to PATH.")
  try:
    if args.neat:
      execute("tred {f} > {f}.0 && mv {f}.0 {f}".format(f=dot_path))
    execute(["dot", dot_path, "-Tpdf", "-o", args.outgraph])
  except Exception as e:
    error("Error generating dependencies with dot: %s: %s", type(e).__name__, e)
  else:
    info("Dependencies graph generated: %s" % args.outgraph)
  if dot_path != args.outdot:
    remove(dot_path)
  else:
    info("Intermediate dot file for Graphviz saved: %s" % args.outdot)
  return True
