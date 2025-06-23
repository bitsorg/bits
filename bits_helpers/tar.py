from shlex import quote
from bits_helpers.cmd import getstatusoutput
from bits_helpers.log import debug
from bits_helpers.scm import SCM, SCMError
import os

GIT_COMMAND_TIMEOUT_SEC = 120
"""Default value for how many seconds to let any git command execute before being terminated."""

GIT_CMD_TIMEOUTS = {
  "clone": 600,
  "checkout": 600
}
"""Customised timeout for some commands."""

def clone_speedup_options():
  """Return a list of options supported by the system git which speed up cloning."""
  for filter_option in ("--filter=tree:0", "--filter=blob:none"):
    _, out = getstatusoutput("LANG=C git clone " + filter_option)
    if "unknown option" not in out and "invalid filter-spec" not in out:
      return [filter_option]
  return []


class Tar(SCM):
  name = "Tar"

  def __init__(self,options,tag):
    # super().__init()
    self.options = options
    self.tag = tag
    self.archive =  os.environ["BITS_SRC_ARCHIVE"]
    
  def checkedOutCommitName(self, directory):
    return git(("rev-parse", "HEAD"), directory)

  def branchOrRef(self, directory):
    out = git(("rev-parse", "--abbrev-ref", "HEAD"), directory=directory)
    if out == "HEAD":
      out = git(("rev-parse", "HEAD"), directory)[:10]
    return out
  
  def exec(self, *args, **kwargs):
    return tar(*args, **kwargs)

  def parseRefs(self, output):
    return {
      git_ref: git_hash for git_hash, sep, git_ref
      in (line.partition("\t") for line in output.splitlines()) if sep
    }

  def listRefsCmd(self, repository):
    return ["ls-remote", "--heads", "--tags", repository]
 
  def cloneReferenceCmd(self, url, referenceRepo, usePartialClone):
    os.system("rm -rf "+referenceRepo)
    os.system("mkdir -p "+referenceRepo)
    cmd ="curl -s "+url+" | tar -C "+referenceRepo+" --strip-components 1 " + self.options + " - "
    os.system(cmd)
    cmd ="(cd "+referenceRepo+" && git init && git add . && git commit -a -m Import && git tag "+str(self.tag) + ")";
    os.system(cmd)
    return["-C",referenceRepo,"status"]

  def cloneSourceCmd(self, source, destination, referenceRepo, usePartialClone):
    #cloneReferenceCmd(self, spec, referenceRepo, usePartialClone)
    return["status"]
 
  def checkoutCmd(self, tag):
    # return ["checkout", "-f", tag]
    return

  def fetchCmd(self, remote, *refs):
     # return ["fetch", "-f"] + [remote, *refs]
     return

  def setWriteUrlCmd(self, url):
    # return ["remote", "set-url", "--push", "origin", url]
    return

  def diffCmd(self, directory):
    # return "cd %s && git diff -r HEAD && git status --porcelain" % directory
    return

  def checkUntracked(self, line):
    # return line.startswith("?? ")
    return


def tar(args, directory=".", check=True, prompt=True):
  lastGitOverride = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
  debug("Executing git %s (in directory %s)", " ".join(args), directory)
  # We can't use git --git-dir=%s/.git or git -C %s here as the former requires
  # that the directory we're inspecting to be the root of a git directory, not
  # just contained in one (and that breaks CI tests), and the latter isn't
  # supported by the git version we have on slc6.
  # Silence cd as shell configuration can cause the new directory to be echoed.
  if not args:
     debug("Skipping empty command")
     return ""
           
  err, output = getstatusoutput("""\
  set -e +x
  cd {directory} >/dev/null 2>&1
  {prompt_var} {directory_safe_var} git {args}
  """.format(
    directory=quote(directory),
    args=" ".join(map(quote, args)),
    # GIT_TERMINAL_PROMPT is only supported in git 2.3+.
    prompt_var="GIT_TERMINAL_PROMPT=0" if not prompt else "",
    directory_safe_var=f"GIT_CONFIG_COUNT={lastGitOverride+2} GIT_CONFIG_KEY_{lastGitOverride}=safe.directory GIT_CONFIG_VALUE_{lastGitOverride}=$PWD GIT_CONFIG_KEY_{lastGitOverride+1}=gc.auto GIT_CONFIG_VALUE_{lastGitOverride+1}=0" if directory else "",
  ), timeout=GIT_CMD_TIMEOUTS.get(args[0] if len(args) else "*", GIT_COMMAND_TIMEOUT_SEC))
  if check and err != 0:
    raise SCMError("Error {} from git {}: {}".format(err, " ".join(args), output))
  return output if check else (err, output)
