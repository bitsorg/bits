try:
    from md5 import new as md5adder
except ImportError:
    from hashlib import md5 as md5adder
from os.path import abspath, join, exists, dirname, basename
from os import rename, unlink
import os
import re
from tempfile import mkdtemp
from subprocess import getstatusoutput
from urllib.request import urlopen, Request
from urllib.error import URLError
import base64
from time import time, sleep
from types import SimpleNamespace
from bits_helpers.log import error, warning, debug, info
from bits_helpers.checksum import check_file
import json


# ---------------------------------------------------------------------------
# Sentinel-file helpers for concurrent prefetch coordination
# ---------------------------------------------------------------------------

def _sentinel_path(path):
    """Return the sentinel file path for *path* (appends '.downloading')."""
    return path + ".downloading"


def _acquire_download(path):
    """Atomically create a sentinel for *path*.

    Returns ``True`` if this caller successfully created the sentinel (i.e.
    this caller owns the download) and ``False`` if another thread/process
    already holds it.  The sentinel contains the current PID so stale files
    from crashed processes can be identified at startup.
    """
    try:
        fd = os.open(_sentinel_path(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _wait_for_sentinel(path):
    """Block until no in-progress download sentinel exists for *path*."""
    sentinel = _sentinel_path(path)
    while os.path.exists(sentinel):
        sleep(0.25)

urlRe = re.compile(r".*:.*/.*")
urlAuthRe = re.compile(r'^(http(s|)://)([^:]+:[^@]+)@(.+)$')


class MalformedUrl(Exception):
    def __init__(self, url, missingParams=[]):
        if not missingParams:
            self.args = ["ERROR: The following url is malformed: %(url)s." % locals()]
        else:
            self.args = ["ERROR: The following parameters are missing from url %(url)s: %(missingParams)s" % locals()]

def format(s, **kwds):
    return s % kwds

def executeWithErrorCheck(command, errorMessage):
    debug(command)
    error, output = getstatusoutput(command)
    if error:
        info(errorMessage + ":")
        info("")
        info(command)
        info("")
        info("resulted in:")
        info(output)
        return False
    debug(output)
    return True


def packCheckout(tempdir, dest, *exports):
    """ Use this helper method when download protocol is like cvs/svn/git
        where the code is checked out in a temporary directory and then tarred
        up.
    """
    export = " ".join(['"%s"' % x for x in exports])
    packCommand = """cd %(tempdir)s; tar -zcf "%(dest)s" %(export)s """
    packCommand = packCommand % locals()
    errorMessage = "Error while creating a tar archive for checked out area"
    return executeWithErrorCheck(packCommand, errorMessage)

# We have our own version rather than using the one from os
# because the latter does not work seem to be thread safe.
def makedirs(path):
    returncode, out = getstatusoutput("mkdir -p {}".format(path))
    if returncode != 0:
        raise OSError("makedirs() failed (return: {}):\n{}".format(returncode, out))

def downloadUrllib2(source, destDir, work_dir, dest_filename=None):
    try:
        dest = "/".join([destDir.rstrip("/"), dest_filename if dest_filename else basename(source)])
        headers={"Cache-Control": "no-cache"}
        m = urlAuthRe.match(source)
        if m:
            source = m.group(1)+m.group(4)
            headers['Authorization'] = "Basic %s" % base64.b64encode(m.group(3))
        req = Request(source, headers=headers)
        s = urlopen(req)
        tmpfile = "{}.{:f}.tmp".format(dest, time())
        f = open(tmpfile, "wb")
        # Read in blocks to avoid using too much memory.
        block_sz = 8192 * 16
        while True:
            buffer = s.read(block_sz)
            if not buffer:
                break
            f.write(buffer)
        f.close()
        if not exists(dest):
            rename(tmpfile, dest)
        else:
            unlink(tmpfile)
    except URLError as e:
        debug("Error while downloading {}: {}".format(source, e))
        return False
    except Exception as e:
        debug("Error while downloading {}: {}".format(source, e))
        return False
    return True

# Download a files from a git url.  We do not clone the remote reposiotory, but
# we simply pull the branch we are interested in and then we drop all the git
# information while creating a tarball.  The syntax to define a repository is
# the following:
#
# git:/local/repository?obj=BRANCH/TAG
# git://remote-repository?obj=BRANCH/TAG
# git+https://remote-repository-over-http/foo.git?obj=BRANCH/TAG
#
# If "obj" does not contain a "/", it's value will be considered a branch and TAG will be "HEAD".
# If "obj" is not specified, it will be "master/HEAD" by default.
# By default export is the <basename of the url without ".git">-TAG unless it is HEAD,
# in which case it will be  <basename of the url without .git>-BRANCH.
# One can specify an additional parameter
#
#     filter=<some-path>
#
# which will be used to pack only a subset of the checkout.

def downloadGit(source, dest, work_dir):
    protocol, gitroot, args = parseGitUrl(source)
    tempdir = createTempDir(work_dir, "tmp")

    exportpath = join(tempdir, args["export"])
    if protocol=="git": protocol="https"
    if protocol:
        protocol += "://"
    if not protocol and not gitroot.endswith(".git"):
        gitroot = join(gitroot, ".git")

    dest = join(dest, args["output"].lstrip("/"))
    args.update({"protocol": protocol, "tempdir": tempdir,
                 "gitroot": gitroot, "dest": dest,
                 "exportpath": exportpath})
    makedirs(exportpath)
    if "submodules" in args:
        args["submodules"] = " git submodule update --recursive --init &&"
    else:
        args["submodules"] = ""
    command = format("cd %(exportpath)s &&"
                     "git init &&"
                     "git pull --tags %(protocol)s%(gitroot)s refs/heads/%(branch)s &&"
                     "git remote add origin %(protocol)s%(gitroot)s &&"
                     "git reset --hard %(tag)s && %(submodules)s"
                     "find . ! -path '%(filter)s' -delete &&"
                     "rm -rf .git .gitattributes .gitignore", **args)
    error, output = getstatusoutput(command % args)
    if error:
        warning("Error while downloading sources from %s using git.\n\n"
            "%s\n\n"
            "resulted in:\n%s" % (gitroot, command % args, output))
        return False
    return packCheckout(args["tempdir"], args["dest"], args["export"])


def parseGitUrl(url):
    protocol, gitroot, args = parseUrl(url, requestedKind="git",
                                       defaults={"obj": "master/HEAD"})
    parts = args["obj"].rsplit("/", 1)
    if len(parts) != 2:
        parts += ["HEAD"]
    args["branch"], args["tag"] = parts

    if not "export" in args:
        args["export"] = basename(re.sub(r"\.git$", "", re.sub(r"[?].*", "", gitroot)))
        if args["tag"] != "HEAD":
            args["export"] += args["tag"]
        else:
            args["export"] += args["branch"]

    if not "output" in args:
        args["output"] = args["export"] + ".tar.gz"
        args["gitroot"] = gitroot
    if not "filter" in args:
        args["filter"] = "*"
    args["filter"] = sanitize(args["filter"])
    return protocol, gitroot, args





def createTempDir(workDir, subDir):
    tempdir = join(workDir, subDir)
    if not exists(tempdir):
        makedirs(tempdir)
    tempdir = mkdtemp(dir=tempdir)
    return tempdir

# Minimal string sanitization.
def sanitize(s):
    return re.sub(r"[^a-zA-Z_0-9*./-]", "", s)

def getUrlChecksum(s):
    m = md5adder(fixUrl(s).encode())
    return m.hexdigest()

def parseUrl(url, requestedKind=None, defaults={}, required=[]):
    match = re.match("([^+:]*)([^:]*)://([^?]*)(.*)", url)
    if not match:
        raise MalformedUrl(url)
    parts = match.groups()
    protocol, deliveryProtocol, server, arguments = match.groups()
    arguments = arguments.strip("?")
    # In case of urls of the kind:
    # git+https://some.web.git.repository.net
    # we consider "https" the actual protocol and
    # "git" merely the request kind.
    if requestedKind and not protocol == requestedKind:
        raise MalformedUrl(url)
    if deliveryProtocol:
        protocol = deliveryProtocol.strip("+")
    arguments.replace("&amp;", "&")
    args = list(defaults.items())
    parsedArgs = re.split("&", arguments)
    parsedArgs = [x.split("=") for x in parsedArgs]
    parsedArgs = [(len(x) != 2 and [x[0], True]) or x for x in parsedArgs]
    args.extend(parsedArgs)
    argsDict = dict(args)
    missingArgs = [arg for arg in required if arg not in argsDict]
    if missingArgs:
        raise MalformedUrl(url, missingArgs)
    return protocol, server, argsDict

def fixUrl(s):
    for x in ['no-cmssdt-cache=1', 'cmdist-generated=1']:
        if x in s:
            s = s.replace(x,'').replace("&&","&").replace("?&","?")
            if s.endswith('&'): s=s[:-1]
            if s.endswith('?'): s=s[:-1]
    return s

def downloadPip(source, dest, work_dir):
    # Valid PIP URL formats are
    # pip://package/version?[pip_options=downloadOptions&][pip=pip_command&][pip_package=package&]output=/tarbalname
    # pip://package/version/tarbalname
    url_parts = source.split("pip://", 1)[1].split("?", 1)
    filename = source.rsplit("/", 1)[1]
    opts = []
    if len(url_parts) > 1: opts = url_parts[1].split("&")
    pkg = url_parts[0].split("/")
    pack = pkg[0].strip()
    tar_names = [pack.replace("-", "_"), pack] if '-' in pack else [pack]
    for tar_name in tar_names:
      pypi_file = '{}-{}.tar.gz'.format(tar_name, pkg[1].strip())
      pypi_url = 'https://pypi.io/packages/source/{}/{}/{}'.format(pack[0], pack, pypi_file)
      if downloadUrllib2(pypi_url, dest, work_dir, dest_filename=filename):
        return
    pack = pack + '==' + pkg[1].strip()
    pip_opts = "--no-deps --no-binary=:all:"
    pip="pip"
    isSourceDownload=True

    for opt in opts:
        if opt.startswith("pip="): pip=opt.split('=',1)[-1]
        elif opt.startswith("pip_options="):
            pip_optsT = opt[12:].replace("+", " ").replace("%20", " ").replace("%3D", "=")
            # hack here a alternative source location
            pip_opts = ''
            spSrc = pip_optsT.split()
            for i in range(len(spSrc)):
                if 'ALTSRC' in spSrc[i]:
                    pack = spSrc[i + 1]
                    i = i + 1
                else:
                    if ("no-binary" in spSrc[i]) or ("only-binary" in spSrc[i]):
                        spSrc[i] = re.sub(',arch=[a-z0-9_]+','',spSrc[i])
                    pip_opts = pip_opts + ' ' + spSrc[i]
                    if "only-binary=:all:" in spSrc[i]:
                        isSourceDownload=False
                    elif "no-binary" in spSrc[i] and "all" not in spSrc[i]:
                        isSourceDownload=False #not totally robust - but basically use pip if source is overridden

    if isSourceDownload:
        debug("Looking for sources at https://pypi.org/pypi/"+pack.split('=')[0]+"/json")
        fj=urlopen("https://pypi.org/pypi/"+pack.split('=')[0]+"/json")
        data=json.load(fj)
        url=None
        if "releases" in data and pack.split('=')[2] in data["releases"]:
            for file in data["releases"][pack.split('=')[2]]:
                if file["packagetype"] == "sdist":
                    url=file["url"]
        if url is not None:
            debug("Found source on pypi - downloading")
            return downloadUrllib2(url, dest, work_dir, dest_filename=filename)

    if not '--no-deps' in pip_opts: pip_opts = '--no-deps ' + pip_opts
    if not '--no-cache-dir' in pip_opts: pip_opts = '--no-cache-dir ' + pip_opts
    comm = 'cd ' + dest + ";" + pip + ' download ' + pip_opts + ' --disable-pip-version-check -q -d . {}; [ -e {} ] || mv *.* {}; ls -l'.format(pack, filename, filename)
    error, output = getstatusoutput(comm)
    return not error

def downloadFile(source, dest, work_dir):
    import shutil
    shutil.copy(source.removeprefix("file:/"), dest)
    return

downloadHandlers = {
    "http": downloadUrllib2,
    "https": downloadUrllib2,
    "ftp": downloadUrllib2,
    "ftps": downloadUrllib2,
    "git": downloadGit,
    "pip": downloadPip,
    "file": downloadFile
}


def download(source, dest, work_dir, checksum=None, enforce_mode="off",
             sync_helper=None):
    """Download *source* into *dest*, optionally verifying its checksum.

    Parameters
    ----------
    source:
        URL to download.  Must be a clean URL with **no** embedded checksum
        suffix (callers should call ``bits_helpers.checksum.parse_entry``
        before passing the URL here).
    dest:
        Directory into which the downloaded file is placed.
    work_dir:
        Top-level work directory (used for the download cache).
    checksum:
        Expected checksum string in ``'algo:hexdigest'`` format, or ``None``
        when the recipe entry carried no checksum declaration.
    enforce_mode:
        One of ``"off"`` (default), ``"warn"``, ``"enforce"``, ``"print"``.
        Passed directly to ``bits_helpers.checksum.check_file``.
    sync_helper:
        Optional sync-backend instance (any class from ``bits_helpers.sync``).
        When provided:

        * A cache miss first attempts ``sync_helper.fetch_source()`` before
          hitting the upstream URL, so the remote store acts as a mirror
          that survives upstream disappearance.
        * A successful upstream download is immediately archived via
          ``sync_helper.upload_source()`` so future builds (on any machine
          with the same remote store) can skip the upstream download.

        Pass ``None`` (the default) to preserve the previous behaviour.
    """
    noCmssdtCache = True if 'no-cmssdt-cache=1' in source else False
    isCmsdistGenerated = True if 'cmdist-generated=1' in source else False
    source = fixUrl(source)
    url_checksum = getUrlChecksum(source)

    # Syntactic sugar to allow the following urls for tag collector:
    #
    # cmstc:[base.]release[.tagset[.tagset[...]]]/src.tar.gz
    #
    # in place of:
    #
    # cmstc://?tag=release&baserel=base&extratag=tagset1,tagset2,..&module=CMSSW&export=src&output=/src.tar.gz
    if source.startswith("cmstc:") and not source.startswith("cmstc://"):
        url = source.split(":", 1)[1]
        desc, output = url.rsplit("/", 1)
        parts = desc.split(".")
        releases = [x for x in parts if not x.isdigit()]
        extratags = [x for x in parts if x.isdigit()]
        if extratags:
            extratags = "&extratags=" + ",".join(extratags)
        if len(releases) == 1:
            baserel = ""
            release = "tag=" + releases[0]
        elif len(releases) == 2:
            baserel = "&baserel=" + releases[0]
            release = releases[1]
        else:
            raise MalformedUrl(source)
        source = "cmstc://?{}{}{}&module=CMSSW&export=src&output=/{}".format(release, baserel, extratags, output)

    cacheDir = abspath(join(work_dir, "SOURCES/cache"))
    urlTypeRe = re.compile(r"([^:+]*)([^:]*)://.*")
    match = urlTypeRe.match(source)
    if not urlTypeRe.match(source):
        raise MalformedUrl(source)
    downloadHandler = downloadHandlers[match.group(1)]
    filename = source.rsplit("/", 1)[1]
    downloadDir = join(cacheDir, url_checksum[0:2], url_checksum)
    try:
        makedirs(downloadDir)
    except OSError as e:
        if not exists(downloadDir):
            raise e

    realFile = join(downloadDir, filename)
    # If a background prefetch thread is currently downloading this file,
    # wait for it to finish before inspecting the cache.
    _wait_for_sentinel(realFile)
    fetched_from_upstream = False
    if not exists(realFile):
        # Before hitting the upstream URL, check whether the remote store
        # already has an archived copy of this source.  This makes rebuilds
        # resilient to upstream URL disappearance.
        if sync_helper is not None:
            debug("Trying remote store for source file: %s", filename)
            sync_helper.fetch_source(url_checksum, filename, downloadDir)

        if not exists(realFile):
            debug("Trying to fetch source file: %s", source)
            downloadHandler(source, downloadDir, work_dir)
            fetched_from_upstream = True

    if exists(realFile):
        # Verify checksum against the cached copy (covers both fresh downloads
        # and cache hits so a corrupted cache entry is caught on the next use).
        check_file(realFile, filename, checksum, enforce_mode)
        # Archive to the write store when the file came from the upstream URL
        # (i.e. it was not already in the local cache or the remote store).
        # This ensures every new download is preserved for future builds.
        if fetched_from_upstream and sync_helper is not None:
            debug("Archiving source file %s to remote store", filename)
            sync_helper.upload_source(realFile, url_checksum, filename)
        executeWithErrorCheck("mkdir -p {dest}; cp {src} {dest}/".format(dest=dest, src=realFile), "Failed to move source")
    else:
        raise OSError("Unable to download source {} in to {}".format(source, downloadDir))
    return
