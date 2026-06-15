# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

# Standard library
import copy
import re


class ResourceManager:
    """Allocate and release build resources (CPU, RSS) for parallel tasks.

    The manager reads per-package resource statistics from a JSON file produced
    by a previous build run.  It uses those statistics to decide which pending
    jobs can start without exceeding the machine's available resources.

    Parameters
    ----------
    ESstats:
        Dictionary loaded from the build-statistics JSON file.  Expected keys:
        ``"resources"`` (dict of resource totals), ``"packages"`` (per-package
        resource estimates), ``"known"`` (regex-based fallback list), and
        ``"defaults"`` (default resource values by index).
    scheduler:
        Scheduler instance; used only for debug logging.
    highestPriorityOnly:
        When ``True``, stop considering further jobs as soon as the
        highest-priority job cannot be allocated (strict head-of-line
        blocking).  Defaults to ``False`` (best-effort packing).
    """

    def __init__(self, ESstats, scheduler, highestPriorityOnly=False):
        self.esStats = ESstats
        self.scheduler = scheduler
        self.machineResources = ESstats["resources"]
        self.resourceList = ["cpu", "rss"]
        self.allocated = {}
        self.highestPriorityOnly = highestPriorityOnly
        self.seenPackages = {}
        self.priorityList = ["time"]  # can be any list from the stat keys
        # Cap per-package resource requirements at the machine totals so that
        # a package can always eventually be scheduled.
        for xtype in self.esStats["packages"]:
            for pkg in self.esStats["packages"][xtype]:
                for res in self.resourceList:
                    if self.esStats["packages"][xtype][pkg][res] > self.machineResources[res]:
                        self.esStats["packages"][xtype][pkg][res] = self.machineResources[res]

    def allocResourcesForExternals(self, externalsList, count=1000):
        """Return an ordered subset of *externalsList* that fits in available resources.

        Jobs are sorted by the configured priority metric (default: build time)
        and greedily allocated until *count* jobs are scheduled or resources are
        exhausted.  Already-seen packages use cached resource estimates.
        """
        externals_to_run = []
        if count <= 0:
            return externals_to_run
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

        # Sort by priority metric(s) then greedily allocate within resource limits.
        externals_ordered = []
        for ex_stats in sorted(externals_to_run,
                               key=lambda x: tuple(x[k] for k in self.priorityList),
                               reverse=True):
            if not [r for r in self.resourceList if ex_stats[r] > self.machineResources[r]]:
                for prm in self.resourceList:
                    self.machineResources[prm] -= ex_stats[prm]
                externals_ordered.append(ex_stats["name"])
                self.allocated[ex_stats["name"]] = ex_stats
                self.scheduler.debug("Allocating resources %s" % ex_stats)
                count -= 1
                if count <= 0:
                    break
            elif self.highestPriorityOnly:
                break
        if externals_ordered:
            self.scheduler.debug("Available resources %s" % self.machineResources)
            self.scheduler.debug("Buildable tasks {}: {}".format(
                len(externals_ordered), ",".join(externals_ordered)))
        return externals_ordered

    def releaseResourcesForExternal(self, external):
        """Return the resources held by *external* to the machine pool."""
        if external not in self.allocated:
            return
        for prm in self.resourceList:
            self.machineResources[prm] += self.allocated[external][prm]
        self.scheduler.debug("Released resources: {} , {}".format(
            self.allocated[external], self.machineResources))
        del self.seenPackages[external]
        del self.allocated[external]
