import json
from queue import Queue, PriorityQueue
from io import StringIO
from threading import Thread
from time import sleep
import threading
import traceback
from bits_helpers.resource_manager import ResourceManager

# Helper class to avoid conflict between result
# codes and quit state transition.
class _SchedulerQuitCommand:
  pass

def transition(what, fromList, toList):
  try:
    fromList.remove(what)
  except ValueError as e:
    print (what + " not in source list")
    raise e
  toList.append(what)

class Scheduler:
  # A simple job scheduler.
  # Workers queue is to specify to threads what to do. Results
  # queue is whatched by the master thread to wait for results
  # of workers computations.
  # All worker threads begin trying to fetch from the command queue (and are
  # therefore blocked).
  # Master thread does the scheduling and then sits waiting for results.
  # Scheduling implies iterating on the list of jobs and creates an entry
  # in the parallel queue for all the jobs which does not have any dependency
  # which is not done.
  # If a job has dependencies which did not build, move it do the failed queue.
  # Post an appropriate build command for all the new jobs which got added.
  # If there are jobs still to be done, post a reschedule job on the command queue
  # if there are no jobs left, post the "kill worker" task.
  # There is one, special, "final-job" which depends on all the scheduled jobs
  # either parallel or serial. This job is guaranteed to be executed last and
  # avoids having deadlocks due to all the queues having been disposed.
  def __init__(self, parallelThreads, logDelegate=None, buildStats=None, parallelDownloads=2):
    self.workersQueue = PriorityQueue()
    self.resultsQueue = Queue()
    self.notifyQueue = Queue()
    self.rescheduleParallel = False
    self.jobs = {}
    self.pendingJobs = []
    self.runningJobs = []
    self.runningJobsCache = []
    self.doneJobs = []
    self.brokenJobs = []
    self.parallelThreads = parallelThreads+parallelDownloads
    self.logDelegate = logDelegate
    self.resourceManager = None
    self.runningJobsCount = {"build": 0, "fetch": 0, "download": 0, "max_build": parallelThreads, "max_download": parallelDownloads}
    self.errors = {}
    if buildStats:
      with open(buildStats) as ref:
        self.resourceManager = ResourceManager(json.load(ref), self)
    # Add a final job, which will depend on any spawned task so that we do not
    # terminate until we are completely done.
    self.finalJobDeps = []
    self.finalJobSpec = [self.doSerial, "final-job", self.finalJobDeps] + [self.__doLog, "Nothing else to be done, exiting."]
    self.resultsQueue.put((threading.currentThread(), self.finalJobSpec))
    self.jobs["final-job"] = {"scheduler": "serial", "deps": self.finalJobSpec, "spec": self.finalJobSpec}
    self.pendingJobs.append("final-job")

  def run(self):
    all_threads = []
    for i in range(self.parallelThreads):
      t = Thread(target=self.__createWorker())
      all_threads.append(t)
      t.daemon = True
      t.start()

    self.__doRescheduleParallel()
    # Wait until all the workers are done.
    while self.parallelThreads:
      try:
        self.__doNotifications()
        who, item = self.resultsQueue.get()
        item[0](*item[1:])
        sleep(0.1)
        if not any([True for t in all_threads if t.is_alive()]):
          break
      except KeyboardInterrupt:
        print ("Ctrl-c received, waiting for workers to finish")
        while self.workersQueue.full():
          self.workersQueue.get(False)
        self.shout(self.quit)

    # Prune the queue.
    self.__doNotifications ()
    while self.resultsQueue.full():
      item = self.resultsQueue.get()
      item[0](*item[1:])
    self.__doNotifications ()
    return

  # Create a worker.
  def __createWorker(self):
    def worker():
      while True:
        pri, taskId, item = self.workersQueue.get()
        try:
          result = item[0](*item[1:])
        except Exception as e:
          s = StringIO()
          traceback.print_exc(file=s)
          result = s.getvalue()

        if type(result) == _SchedulerQuitCommand:
          self.notifyTaskMaster(self.__releaseWorker)
          return
        self.debug(taskId + ":" + str(item[0]) + " done")
        self.notifyMaster(self.__updateJobStatus, taskId, result)
        if (taskId in self.jobs) and ("taskType" in self.jobs[taskId]):
          taskType = self.jobs[taskId]["taskType"]
          if self.resourceManager and taskType == "build":
            self.notifyMaster(self.resourceManager.releaseResourcesForExternal, taskId)
        self.notifyMaster(self.__rescheduleParallel)
    return worker

  def __doNotifications(self):
    self.rescheduleParallel = False
    while self.notifyQueue.qsize():
      who, item = self.notifyQueue.get()
      item[0](*item[1:])
    if self.rescheduleParallel:
       self.__doRescheduleParallel()

  def __releaseWorker(self):
    self.parallelThreads -= 1

  def parallel(self, taskId, deps, taskType, *spec):
    if taskId in self.jobs: return
    self.jobs[taskId] = {"taskType": taskType, "scheduler": "parallel", "deps": deps, "spec":spec, "priorty": 1}
    if taskType in ["build", "download", "fetch"]:
      try:
          self.jobs[taskId]["priorty"] = 100000-spec[1].requiredBy
      except:
          self.jobs[taskId]["priorty"] = 1
    self.pendingJobs.append(taskId)
    self.finalJobDeps.append(taskId)

  # Does the rescheduling of tasks. Derived class should call it.
  def __rescheduleParallel(self):
    self.rescheduleParallel = True

  def __doRescheduleParallel(self):
    parallelJobs = [j for j in self.pendingJobs if self.jobs[j]["scheduler"] == "parallel"]
    # First of all clean up the pending parallel jobs from all those
    # which have broken dependencies.
    for taskId in parallelJobs:
      brokenDeps = [dep for dep in self.jobs[taskId]["deps"] if dep in self.brokenJobs]
      if not brokenDeps:
        continue
      transition(taskId, self.pendingJobs, self.brokenJobs)
      self.errors[taskId] = "The following dependencies could not complete:\n%s" % "\n".join(brokenDeps)

    # If no tasks left, quit. Notice we need to check also for serial jobs
    # since they might queue more parallel payloads.
    if not self.pendingJobs:
      self.shout(self.quit)
      self.notifyTaskMaster(self.quit)
      return

    # Otherwise do another round of scheduling of all the tasks. In this
    # case we only queue parallel jobs to the parallel queue.
    if self.runningJobsCache != self.runningJobs:
      self.runningJobsCache = self.runningJobs[:]

    allJobs =[]
    for taskId in parallelJobs:
      pendingDeps = [dep for dep in self.jobs[taskId]["deps"] if not dep in self.doneJobs]
      if pendingDeps:
        continue
      allJobs.append({"id": taskId, "priorty": self.jobs[taskId]["priorty"]})
    buildJobs =[]
    downloadJobs = []
    forceJobs = []
    bldCount = self.runningJobsCount["max_build"]-self.runningJobsCount["build"]
    dwnCount = self.runningJobsCount["max_download"]-self.runningJobsCount["download"]
    for task in sorted(allJobs, key=lambda k: k['priorty']):
      taskId = task["id"]
      taskType = self.jobs[taskId]["taskType"]
      if taskType == "download":
        if dwnCount>0:
          downloadJobs.append(taskId)
          dwnCount -= 1
      elif taskType == "build":
        if bldCount>0: #include all build jobs so that we can match those which can be run
          buildJobs.append(taskId)
      else:
        forceJobs.append(taskId)
    if bldCount>0 and buildJobs:
      if self.resourceManager:
        buildJobs = self.resourceManager.allocResourcesForExternals(buildJobs, count=bldCount)
      else:
        buildJobs = buildJobs[:bldCount]
    for taskId in forceJobs + downloadJobs + buildJobs:
      taskType = self.jobs[taskId]["taskType"]
      if taskType in self.runningJobsCount:
        self.runningJobsCount[taskType] += 1
      transition(taskId, self.pendingJobs, self.runningJobs)
      self.__scheduleParallel(taskId, self.jobs[taskId]["spec"], priorty=self.jobs[taskId]["priorty"])

  # Update the job with the result of running.
  def __updateJobStatus(self, taskId, error):
    if "taskType" in self.jobs[taskId]:
      taskType = self.jobs[taskId]["taskType"]
      if taskType in self.runningJobsCount:
        self.runningJobsCount[taskType] -= 1
    if not error:
      transition(taskId, self.runningJobs, self.doneJobs)
      return
    transition(taskId, self.runningJobs, self.brokenJobs)
    self.errors[taskId] = error

  # One task at the time.
  def __scheduleParallel(self, taskId, commandSpec, priorty=1):
    self.workersQueue.put((priorty, taskId, commandSpec))

  # Helper to enqueue commands for all the threads.
  def shout(self, *commandSpec):
    for x in range(self.parallelThreads):
      self.__scheduleParallel("quit-" + str(x), commandSpec)

  # Helper to enqueu replies to the master thread.
  def notifyTaskMaster(self, *commandSpec):
    self.resultsQueue.put((threading.currentThread(), commandSpec))

  def notifyMaster(self, *commandSpec):
    self.notifyQueue.put((threading.currentThread(), commandSpec))

  def forceDone(self, taskId):
    if taskId in self.doneJobs: return
    if not taskId in self.jobs: self.jobs[taskId]={}
    if not taskId in self.pendingJobs: self.pendingJobs.append(taskId)
    transition(taskId, self.pendingJobs, self.doneJobs)

  def serial(self, taskId, deps, *commandSpec):
    if taskId in self.jobs: return
    spec = [self.doSerial, taskId, deps] + list(commandSpec)
    self.resultsQueue.put((threading.currentThread(), spec))
    self.jobs[taskId] = {"scheduler": "serial", "deps": deps, "spec": spec}
    self.pendingJobs.append(taskId)
    self.finalJobDeps.append(taskId)

  def doSerial(self, taskId, deps, *commandSpec):
    pendingDeps = [dep for dep in deps if not dep in self.doneJobs]
    brokenDeps = [dep for dep in deps if dep in self.brokenJobs]
    if brokenDeps:
      #put back if there are other pending tasks
      if [dep for dep in pendingDeps if not dep in brokenDeps]:
        self.resultsQueue.put((threading.currentThread(), [self.doSerial, taskId, deps] + list(commandSpec)))
        return
      transition(taskId, self.pendingJobs, self.brokenJobs)
      self.errors[taskId] = "The following dependencies could not complete:\n%s" % "\n".join(brokenDeps)
      # Remember to do the scheduling again!
      self.notifyMaster(self.__rescheduleParallel)
      return

    # Put back the task on the queue, since it has pending dependencies.
    if pendingDeps:
      self.resultsQueue.put((threading.currentThread(), [self.doSerial, taskId, deps] + list(commandSpec)))
      return
    # No broken dependencies and no pending ones. Run the job.
    if not (taskId in self.doneJobs):
      transition(taskId, self.pendingJobs, self.runningJobs)
      try:
        result = commandSpec[0](*commandSpec[1:])
      except Exception as e:
        s = StringIO()
        traceback.print_exc(file=s)
        result = s.getvalue()
      self.__updateJobStatus(taskId, result)
    # Remember to do the scheduling again!
    self.notifyMaster(self.__rescheduleParallel)

  # Helper method to do logging:
  def log(self, s):
    if self.logDelegate:
      self.notifyMaster(self.logDelegate.info, s)
    else:
      self.__doLog(s)

  def debug(self, s):
    if self.logDelegate:
      self.notifyMaster(self.logDelegate.debug, s)
    else:
      self.__doLog(s)

  # Task which forces a worker to quit.
  def quit(self):
    self.debug("Requested to quit.")
    return _SchedulerQuitCommand()

  # Helper for printouts.
  def __doLog(self, s, level=0):
    print (s)

  def reschedule(self):
    self.notifyMaster(self.__rescheduleParallel)

