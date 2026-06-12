import json
from os.path import dirname, abspath, join
from time import sleep
from bits_helpers.scheduler import Scheduler 

def dummyTask():
  sleep(0.1)

def dummyTaskLong():
  sleep(1)

def errorTask():
  return "This will always have an error"

def exceptionTask():
  raise Exception("foo")

def systemExitTask():
  # Mimics a build task that calls dieOnError(), which raises SystemExit.
  raise SystemExit(1)

# Mimics workflow.
def scheduleMore(scheduler):
  scheduler.parallel("download", [], "build",dummyTask)
  scheduler.parallel("build", ["download"], "build",dummyTask)
  scheduler.serial("install", ["build"], dummyTask)

def random_dag(nodes, edge_prob=0.3):
    """
    Generate a random DAG from a list of node names.
    Args:
        nodes (list[str]): List of node identifiers (strings).
        edge_prob (float): Probability of adding a directed edge.
                           Range 0.0–1.0. Default=0.3.
    Returns:
        dict: {node: [list of children]}
    """
    # Shuffle to create an implicit topological order
    import random
    order = nodes[:]
    random.shuffle(order)
    dag = {node: [] for node in order}
    # For each node, only create edges to nodes that appear *later* in the order
    for i, src in enumerate(order):
        for dst in order[i+1:]:
            if random.random() < edge_prob:
                dag[src].append(dst)
    return dag

def test_resource_monitor():
  pkg_resources_file = join(dirname(dirname(abspath(__file__))), "tests", "package_resources.json")
  stats = {}
  with open(pkg_resources_file) as ref:
    stats = json.load(ref)
  init_resources = stats["resources"]
  all_jobs = sorted(["build:%s" % p for p in stats["packages"]["build"]])
  for builders in [1,2,4,8,16]:
    scheduler = Scheduler(builders, buildStats=pkg_resources_file)
    dag = random_dag(all_jobs)
    for pkg in dag:
      scheduler.parallel(pkg , dag[pkg], "build", dummyTask)
    scheduler.run()
    for res in init_resources:
      assert(scheduler.resourceManager.machineResources[res]==init_resources[res])
    doneJobs = sorted(scheduler.doneJobs)
    assert("final-job" in doneJobs)
    doneJobs.remove("final-job")
    assert(all_jobs==doneJobs)
    assert(len(scheduler.resourceManager.seenPackages)==0)
    assert(len(scheduler.resourceManager.allocated)==0)
  return

def test_systemexit_task_does_not_hang():
  """A task that raises SystemExit (e.g. via dieOnError) must be reported as a
  broken job and must NOT hang the scheduler.

  Regression test: previously the worker's ``except Exception`` did not catch
  SystemExit, so the worker thread died without reporting status, the job was
  stuck in runningJobs forever, and scheduler.run() never returned (the build
  hung at the end with no summary).
  """
  import threading
  scheduler = Scheduler(2, parallelDownloads=0)
  scheduler.parallel("build:ok", [], "build", dummyTask)
  scheduler.parallel("build:dies", [], "build", systemExitTask)
  t = threading.Thread(target=scheduler.run, daemon=True)
  t.start()
  t.join(timeout=15)
  assert not t.is_alive(), "scheduler.run() hung on a SystemExit-raising task"
  assert "build:dies" in scheduler.brokenJobs
  assert "build:ok" in scheduler.doneJobs
  assert scheduler.errors.get("build:dies")


def test_critical_path_default_on_and_opt_out():
  # New behaviour is the default; it can be turned off.
  assert Scheduler(2, parallelDownloads=0).criticalPath is True
  assert Scheduler(2, parallelDownloads=0, criticalPath=False).criticalPath is False


def test_critical_path_cold_start_is_depth():
  # With no recorded times every build weighs the same, so the critical-path
  # weight is the longest chain of build edges to the sink (graph depth).
  # Chain: a <- b <- c  (c depends on b depends on a).
  s = Scheduler(4, parallelDownloads=0)             # no buildStats -> cold start
  s.parallel("build:a", [], "build", dummyTask)
  s.parallel("build:b", ["build:a"], "build", dummyTask)
  s.parallel("build:c", ["build:b"], "build", dummyTask)
  w = s.compute_critical_paths()
  assert w["build:a"] == 3.0    # a + b + c
  assert w["build:b"] == 2.0    # b + c
  assert w["build:c"] == 1.0    # c (its only consumer is the zero-cost sink)
  # Larger weight => smaller (more negative) priority => dispatched first.
  assert s.jobs["build:a"]["priority"] < s.jobs["build:b"]["priority"] < s.jobs["build:c"]["priority"]


def test_critical_path_uses_history():
  # Two independent deps of one target, same depth. History makes the slow one
  # heavier, so it outranks the fast one despite equal depth.
  s = Scheduler(4, parallelDownloads=0)
  s._buildTimes = {"fast": 1.0, "slow": 100.0, "top": 1.0}
  s.parallel("build:fast", [], "build", dummyTask)
  s.parallel("build:slow", [], "build", dummyTask)
  s.parallel("build:top", ["build:fast", "build:slow"], "build", dummyTask)
  w = s.compute_critical_paths()
  assert w["build:slow"] > w["build:fast"]          # heavy branch is more critical
  assert w["build:slow"] == 101.0                   # slow(100) + top(1)
  assert w["build:fast"] == 2.0                     # fast(1)  + top(1)
  # The terminal target sinks last.
  assert w["build:top"] < w["build:slow"]
  assert s.jobs["build:slow"]["priority"] < s.jobs["build:fast"]["priority"]


if __name__ == "__main__":
  scheduler = Scheduler(10)
  scheduler.run()

  scheduler = Scheduler(1)
  scheduler.run()

  scheduler = Scheduler(10)
  scheduler.parallel("test", [], "build", scheduler.log, "This is england");
  scheduler.run()

  scheduler = Scheduler(1)
  for x in range(10):
    scheduler.parallel("test", [], "build", dummyTask)
    scheduler.serial("test", [], dummyTask)
  scheduler.run()
  # Notice we have only 2 jobs because there is always a toplevel one
  # which depends on all the others.
  assert(len(scheduler.brokenJobs) == 0)
  assert(len(scheduler.jobs) == 2)

  scheduler = Scheduler(10)
  for x in range(50):
    scheduler.parallel("test" + str(x), [], "build", dummyTask)
  scheduler.run()
  # Notice we have 51 jobs because there is always a toplevel one
  # which depends on all the others.
  assert(len(scheduler.brokenJobs) == 0)
  assert(len(scheduler.jobs) == 51)

  scheduler = Scheduler(1)
  scheduler.parallel("test", [], "build", errorTask)
  scheduler.run()
  # Again, since the toplevel one always depend on all the others
  # it is always broken if something else is brokend.
  assert(len(scheduler.brokenJobs) == 2)
  assert(len(scheduler.runningJobs) == 0)
  assert(len(scheduler.doneJobs) == 0)

  # Check dependency actually works.
  scheduler = Scheduler(10)
  scheduler.parallel("test2", ["test1"], "build", dummyTask)
  scheduler.parallel("test1", [], "build", dummyTaskLong)
  scheduler.run()
  assert(scheduler.doneJobs == ["test1", "test2", "final-job"])

  # Check dependency actually works.
  scheduler = Scheduler(10)
  scheduler.parallel("test3", ["test2"], "build",dummyTask)
  scheduler.parallel("test2", ["test1"], "build",errorTask)
  scheduler.parallel("test1", [], "build",dummyTaskLong)
  scheduler.run()
  assert(scheduler.doneJobs == ["test1"])
  assert(scheduler.brokenJobs == ["test2", "test3", "final-job"])

  # Check ctrl-C will exit properly.
  scheduler = Scheduler(2)
  for x in range(250):
    scheduler.parallel("test" + str(x), [], "build",dummyTask)
  print ("Print Control-C to continue")
  scheduler.run()

  # Handle tasks with exceptions.
  scheduler = Scheduler(2)
  scheduler.parallel("test", [], "build", exceptionTask)
  scheduler.run()
  assert(scheduler.errors["test"])

  # Handle tasks which depend on tasks with exceptions.
  scheduler = Scheduler(2)
  scheduler.parallel("test0", [], "build",dummyTask)
  scheduler.parallel("test1", [], "build",exceptionTask)
  scheduler.parallel("test2", ["test1"], "build", dummyTask)
  scheduler.run()
  assert(scheduler.errors["test1"])
  assert(scheduler.errors["test2"])

  # Handle serial execution tasks.
  scheduler = Scheduler(2)
  scheduler.serial("test0", [], dummyTask)
  scheduler.run()
  assert(scheduler.doneJobs == ["test0", "final-job"])

  # Handle serial execution tasks, one depends from
  # the previous one.
  scheduler = Scheduler(2)
  scheduler.serial("test0", [], dummyTask)
  scheduler.serial("test1", ["test0"], dummyTask)
  scheduler.run()
  assert(scheduler.doneJobs == ["test0", "test1", "final-job"])

  # Serial tasks depending on one another.
  scheduler = Scheduler(2)
  scheduler.serial("test1", ["test0"], dummyTask)
  scheduler.serial("test0", [], dummyTask)
  scheduler.run()
  assert(scheduler.doneJobs == ["test0", "test1", "final-job"])

  # Serial and parallel tasks being scheduled at the same time.
  scheduler = Scheduler(2)
  scheduler.serial("test1", ["test0"], dummyTask)
  scheduler.serial("test0", [], dummyTask)
  scheduler.parallel("test2", [], "build",dummyTask)
  scheduler.parallel("test3", [], "build",dummyTask)
  scheduler.run()
  scheduler.doneJobs.sort()
  assert(scheduler.doneJobs == ["final-job", "test0", "test1", "test2", "test3"])

  # Serial and parallel tasks. Parallel depends on serial.
  scheduler = Scheduler(2)
  scheduler.serial("test1", ["test0"], dummyTask)
  scheduler.serial("test0", [], dummyTask)
  scheduler.parallel("test2", ["test1"], "build",dummyTask)
  scheduler.parallel("test3", ["test2"], "build",dummyTask)
  scheduler.run()
  assert(scheduler.doneJobs == ["test0", "test1", "test2", "test3", "final-job"])

  # Serial task scheduling two parallel task and another dependent
  # serial task. This is actually what needs to be done for building
  # packages. I.e.
  # The first serial task is responsible for checking if a package is already there,
  # then it queues a parallel download sources task, a subsequent build sources
  # one and finally the install built package one.
  scheduler = Scheduler(3)
  scheduler.serial("check-pkg", [], scheduleMore, scheduler)
  scheduler.run()
  assert(scheduler.doneJobs == ["check-pkg", "download", "build", "install", "final-job"])

  # Handle tests with build resources (cpu, rss) requirement
  test_resource_monitor()
