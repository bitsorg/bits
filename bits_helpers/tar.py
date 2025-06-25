from shlex import quote
from bits_helpers.cmd import getstatusoutput
from bits_helpers.log import debug
from bits_helpers.scm import SCM, SCMError
from bits_helpers.git import git, clone_speedup_options
import os

class Tar(SCM):
  name = "Tar"

  def __init__(self,options,tag):
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
    return git(*args, **kwargs)

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
    cmd = ["clone", "-n", referenceRepo, destination]
    if referenceRepo:
      cmd.extend(["--dissociate", "--reference", referenceRepo])
    if usePartialClone:
      cmd.extend(clone_speedup_options())
    return cmd

  def checkoutCmd(self, tag):
    return ["checkout", "-f", tag]

