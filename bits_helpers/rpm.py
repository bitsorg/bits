import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from os.path import abspath, dirname, join, relpath

from bits_helpers.log import banner, error


def clean(text):
  return re.sub(r"[^A-Za-z0-9._+]", "_", text) or "0"


def version_dir(pkg):
  return pkg["version"] + ("-" + pkg["revision"] if pkg.get("revision") else "")


def work_path(pkg):
  parts = [pkg["effective_architecture"], pkg.get("pkg_family"), pkg["name"], version_dir(pkg)]
  return "/".join(p for p in parts if p)


def rpm_path(pkg):
  parts = [pkg.get("pkg_family"), pkg["name"], "%s-%s" % (pkg["version"], pkg["hash"][:12]),
           pkg["effective_architecture"]]
  return "/".join(p for p in parts if p)


def release_path(view, pkg):
  parts = [view, pkg.get("pkg_family"), pkg["name"], pkg["version"], pkg["effective_architecture"]]
  return "/".join(p for p in parts if p)


def rpm_name(pkg):
  return "%s-%s_%s_%s" % (clean(pkg["name"]), pkg["hash"][:12], clean(pkg["version"]),
                          clean(pkg["effective_architecture"]))


def read_meta(root):
  try:
    with open(join(root, ".meta.json")) as f:
      return json.load(f)
  except OSError:
    return None


def dependencies(meta, kind):
  deps = meta["dependencies"][kind]
  return list({d["hash"]: d for d in deps["runtime"] + deps["build"]}.values())


def stage(root, meta, prefix, top):
  from bits_helpers.cvmfs_publish import _pkginfo, relocate_dependencies

  pkg = meta["package"]
  pkgs = [pkg] + dependencies(meta, "recursive")
  dest = join(top, "root" + prefix, rpm_path(pkg))
  shutil.copytree(root, dest, symlinks=True)

  work_dir = _pkginfo(root)[0]
  targets = {"%s/%s" % (work_dir, work_path(p)): "%s/%s" % (prefix, rpm_path(p)) for p in pkgs}
  targets["%s/INSTALLROOT/%s/%s" % (work_dir, pkg["hash"], work_path(pkg))] = "%s/%s" % (
    prefix, rpm_path(pkg))
  relocate_dependencies(dest, targets)

  init = join(dest, "etc", "profile.d", "init.sh")
  with open(init) as f:
    text = f.read()
  for p in pkgs:
    old = "/".join(work_path(p).split("/")[1:])
    new = '"%s/%s"' % (prefix, rpm_path(p))
    text = re.sub(r'"\$WORK_DIR/[^"]*"/%s(?![\w.+~-])' % re.escape(old), lambda _: new, text)
  with open(init, "w") as f:
    f.write(text)


def stage_view(view, packages, prefix, top):
  base = join(top, "root" + prefix)
  for _, m in packages:
    link = join(base, release_path(view, m["package"]))
    os.makedirs(dirname(link))
    os.symlink(relpath(join(base, rpm_path(m["package"])), dirname(link)), link)


def spec(meta, prefix, root):
  pkg = meta["package"]
  path = rpm_path(pkg)
  lines = [
    "%define debug_package %{nil}",
    "%define __os_install_post %{nil}",
    "%define __arch_install_post %{nil}",
    "%define _build_id_links none",
    "Name: " + rpm_name(pkg),
    "Version: " + clean(pkg["version"]),
    "Release: " + clean(pkg["revision"]),
    "Summary: %s built by bits" % rpm_name(pkg),
    "License: NOASSERTION",
    "Prefix: " + prefix,
    "AutoReqProv: no",
  ]
  if pkg["effective_architecture"] == "share":
    lines.append("BuildArch: noarch")
  lines += ["Requires: " + rpm_name(d) for d in dependencies(meta, "direct")]
  lines += [
    "%description",
    "Source %s/%s/etc/profile.d/init.sh to use it." % (prefix, path),
    "%install",
    "mkdir -p %{buildroot}",
    'cp -a "%s/." %%{buildroot}' % root,
    "%post",
    'if [ "$RPM_INSTALL_PREFIX" != "%s" ]; then' % prefix,
    '  cd "$RPM_INSTALL_PREFIX/%s" && xargs -r -d "\\n" sed -i "s|%s/|$RPM_INSTALL_PREFIX/|g"'
    ' < etc/profile.d/.bits-relocate' % (path, prefix),
    "fi",
    "%postun",
    'if [ "$1" = 0 ] && cd "${RPM_INSTALL_PREFIX:-%s}"; then' % prefix,
    '  rm -rf "%s"' % path,
    '  rmdir -p --ignore-fail-on-non-empty "%s" || :' % dirname(path),
    "fi",
    "%files",
    "%defattr(-,root,root,-)",
    '"%s/%s"' % (prefix, path),
  ]
  return "\n".join(lines) + "\n"


def view_spec(view, meta, packages, prefix, root):
  pkg = meta["package"]
  lines = [
    "Name: bits-view-%s_%s" % (clean(view), clean(pkg["effective_architecture"])),
    "Version: " + time.strftime("%Y%m%d%H%M%S"),
    "Release: " + pkg["hash"][:12],
    "Summary: the %s view, built by bits" % clean(view),
    "License: NOASSERTION",
    "BuildArch: noarch",
    "Prefix: " + prefix,
  ]
  lines += ["Requires: " + rpm_name(m["package"]) for _, m in packages]
  lines += [
    "%description",
    "Every package of the %s view, linked from %s/%s." % (clean(view), prefix, view),
    "%install",
    "mkdir -p %{buildroot}",
    'cp -a "%s/." %%{buildroot}' % root,
    "%files",
    "%defattr(-,root,root,-)",
    '"%s/%s"' % (prefix, view),
  ]
  return "\n".join(lines) + "\n"


def rpmbuild(text, top, out):
  spec_file = join(top, "package.spec")
  with open(spec_file, "w") as f:
    f.write(text)
  subprocess.run(["rpmbuild", "--quiet", "-bb", "--define", "_topdir " + top, spec_file],
                 check=True)
  made, = glob.glob(join(top, "RPMS", "*", "*.rpm"))
  return shutil.move(made, join(out, os.path.basename(made)))


def build(root, meta, prefix, out):
  banner("Making the RPM of %s %s", meta["package"]["name"], version_dir(meta["package"]))
  with tempfile.TemporaryDirectory(prefix="bits-rpm-") as top:
    stage(root, meta, prefix, top)
    return rpmbuild(spec(meta, prefix, join(top, "root")), top, out)


def repo_file(url, signed):
  lines = ["[bits]", "name=bits", "baseurl=" + url, "enabled=1",
           "gpgcheck=%d" % signed, "repo_gpgcheck=%d" % signed]
  if signed:
    lines.append("gpgkey=%s/RPM-GPG-KEY-bits" % url)
  return "\n".join(lines) + "\n"


def fingerprints(*args):
  out = subprocess.run(["gpg", "--batch", "--with-colons"] + list(args), capture_output=True,
                       text=True).stdout
  return {line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:")}


def signing_problem(repo, key):
  key_file = join(repo, "RPM-GPG-KEY-bits")
  if os.path.exists(key_file):
    if not key or fingerprints("--list-keys", key) != fingerprints("--show-keys", key_file):
      return "%s is signed: use --sign with the key in %s" % (repo, key_file)
  elif key and glob.glob(join(repo, "*.rpm")):
    return "%s is not signed: publish to it without --sign" % repo


def published_url(repo):
  try:
    with open(join(repo, "bits.repo")) as f:
      return re.search(r"^baseurl=(.*)$", f.read(), re.M).group(1)
  except (OSError, AttributeError):
    return None


def prefix_of(path):
  return subprocess.run(["rpm", "-qp", "--qf", "%{PREFIXES}", path], capture_output=True,
                        text=True).stdout


def publish(rpms, repo, url, key):
  banner("Publishing %d RPMs to %s", len(rpms), repo)
  os.makedirs(repo, exist_ok=True)
  for r in rpms:
    there = join(repo, os.path.basename(r))
    if os.path.exists(there) and prefix_of(there) != prefix_of(r):
      error("%s is already in %s, with the prefix %s", os.path.basename(r), repo, prefix_of(there))
      sys.exit(1)
  for r in rpms:
    there = join(repo, os.path.basename(r))
    if not os.path.exists(there):
      shutil.copy(r, there + ".part")
      os.replace(there + ".part", there)
  subprocess.run(["createrepo_c", "-q", "--update", repo], check=True)
  if key:
    gpg = ["gpg", "--batch", "--yes", "--armor", "--local-user", key]
    subprocess.run(gpg + ["--detach-sign", join(repo, "repodata", "repomd.xml")], check=True)
    subprocess.run(gpg + ["--output", join(repo, "RPM-GPG-KEY-bits"), "--export", key], check=True)
  text = repo_file(url or published_url(repo) or "file://" + repo, bool(key))
  with open(join(repo, "bits.repo"), "w") as f:
    f.write(text)


def doRpm(args, parser):
  from bits_helpers.publish import _find_installroot

  prefix = args.rpmPrefix.rstrip("/")
  if not os.path.isabs(prefix):
    parser.error("--prefix must be an absolute path")
  if args.rpmView and not re.match(r"^[A-Za-z0-9._+-]+$", args.rpmView):
    parser.error("--view must be a name of letters, digits and ._+-")
  if args.rpmRepoUrl and not args.rpmRepo:
    parser.error("--repo-url needs --repo")
  needs = ["rpmbuild"] + ["createrepo_c"] * bool(args.rpmRepo) + ["rpmsign", "gpg"] * bool(
    args.rpmSignKey)
  missing = [n for n in needs if not shutil.which(n)]
  if missing:
    error("bits rpm needs %s: dnf install rpm-build rpm-sign createrepo_c gnupg2",
          " ".join(missing))
    sys.exit(1)
  if args.rpmRepo:
    problem = signing_problem(abspath(args.rpmRepo), args.rpmSignKey)
    if problem:
      parser.error(problem)

  work_dir = abspath(args.workDir)
  root = _find_installroot(work_dir, args.architecture, args.package, args.version)
  meta = read_meta(root)
  if not meta:
    error("%s has no .meta.json", root)
    sys.exit(1)
  packages = [(root, meta)]
  for dep in dependencies(meta, "recursive"):
    dep_root = join(work_dir, work_path(dep))
    dep_meta = read_meta(dep_root)
    if not dep_meta or dep_meta["package"]["hash"] != dep["hash"]:
      error("%s %s is not installed in %s: build it here first", dep["name"],
            version_dir(dep), work_dir)
      sys.exit(1)
    packages.append((dep_root, dep_meta))

  out = abspath(args.rpmOutputDir or join(work_dir, "RPMS", rpm_name(meta["package"])))
  os.makedirs(out, exist_ok=True)
  rpms = [build(r, m, prefix, out) for r, m in packages]
  if args.rpmView:
    banner("Making the RPM of the %s view", args.rpmView)
    with tempfile.TemporaryDirectory(prefix="bits-rpm-") as top:
      stage_view(args.rpmView, packages, prefix, top)
      rpms.append(rpmbuild(view_spec(args.rpmView, meta, packages, prefix, join(top, "root")),
                           top, out))
  if args.rpmSignKey:
    env = {"GPG_TTY": os.ttyname(0) if os.isatty(0) else "", **os.environ}
    subprocess.run(["rpmsign", "--define", "_gpg_name " + args.rpmSignKey, "--addsign"] + rpms,
                   check=True, stdout=subprocess.DEVNULL, env=env)
  if args.rpmRepo:
    repo = abspath(args.rpmRepo)
    publish(rpms, repo, args.rpmRepoUrl, args.rpmSignKey)
    banner("Repository in %s. To use it: cp %s/bits.repo /etc/yum.repos.d/", repo, repo)
  else:
    banner("RPMs in %s. To install them: dnf install %s/*.rpm", out, out)
