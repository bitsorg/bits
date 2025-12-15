import re, copy
class ResourceManager:
    def __init__(self, ESstats, scheduler, highestPriortyOnly = False):
        self.esStats = ESstats
        self.scheduler = scheduler
        self.machineResources = ESstats["resources"]
        self.resouceList = ["cpu", "rss"]
        self.allocated = {}
        self.highestPriortyOnly = highestPriortyOnly
        self.seenPackages = {}
        self.priorityList = ["time"] # can be any list from the stat keys
        # Make sure required package resources are not larger
        # then systems available resources
        for xtype in self.esStats["packages"]:
          for pkg in self.esStats["packages"][xtype]:
            for res in self.resouceList:
              if self.esStats["packages"][xtype][pkg][res] > self.machineResources[res]:
                self.esStats["packages"][xtype][pkg][res] = self.machineResources[res]
        return

    def allocResourcesForExternals(self, externalsList, count=1000): # return ordered list for externals that can be started
        externals_to_run = []
        if count<=0: return externals_to_run
        for ext_full in externalsList:
          stats = {"name": ext_full}
          ext_items = ext_full.split(":", 1)
          ext = ext_items[-1].lower()
          build_type = ext_items[0] if ext_items[0] in ["prep", "build", "install", "srpm", "rpms"] else "build"
          pkg_stats = self.esStats["packages"].get(build_type, {})
          if ext_full in self.seenPackages:
            stats = self.seenPackages[ext_full]
          else:
            if ext not in pkg_stats:
              idx = -1
              ext = "{}:{}".format(build_type, ext)
              for exp in self.esStats["known"]:
                if re.match(exp[0], ext):
                  idx = exp[1]
                  break
              for k in self.esStats["defaults"]:
                stats[k] = self.esStats["defaults"][k][idx]
              self.scheduler.debug("New external found, creating default entry %s" % stats)
            else:
              for k in self.esStats["defaults"]:
                stats[k] = pkg_stats[ext][k]
            self.seenPackages[ext_full] = copy.deepcopy(stats)
          externals_to_run.append(stats)

        # first order them by metric and then run over to alloc resources
        externalsList_sorted = [ext for ext in sorted(externals_to_run, key=lambda x: tuple(x[k] for k in self.priorityList), reverse=True)]
        externals_ordered = []
        for ex_stats in externalsList_sorted:
          if not [r for r in self.resouceList if ex_stats[r]>self.machineResources[r]]:
            for prm in self.resouceList:
              self.machineResources[prm] -= ex_stats[prm]
            externals_ordered.append(ex_stats["name"])
            self.allocated[ex_stats["name"]] = ex_stats
            self.scheduler.debug("Allocating resources %s" % ex_stats)
            count-=1
            if count<=0:
              break
          elif self.highestPriortyOnly:
            break
        if externals_ordered:
          self.scheduler.debug("Available resources %s" % self.machineResources)
          self.scheduler.debug("Buildable tasks {}: {}".format(len(externals_ordered), ",".join(externals_ordered)))
        return externals_ordered

    def releaseResourcesForExternal(self, external):
        if external not in self.allocated: return
        for prm in self.resouceList:
            self.machineResources[prm] +=  self.allocated[external][prm]
        self.scheduler.debug("Released resources: {} , {}".format(self.allocated[external], self.machineResources))
        del self.seenPackages[external]
        del self.allocated[external]
