from __future__ import division, print_function

import os
import abc
import shutil
import numpy as np

from pprint import pprint
from .deltaworks import DeltaFactory

from pymatgen.util.num_utils import sort_dict
from pymatgen.serializers.json_coders import json_pretty_dump
from pymatgen.io.abinitio.task import RunMode
from pymatgen.io.abinitio.pseudos import Pseudo
from pymatgen.io.abinitio.launcher import SimpleResourceManager
from pymatgen.io.abinitio.calculations import PPConvergenceFactory

################################################################################


class DojoError(Exception):
    """Base Error class for DOJO calculations."""


class Dojo(object):
    """
    This object drives the execution of the tests for the pseudopotential.

    A Dojo has a set of masters, each master is associated to a particular trial
    and is responsilble for the validation/rating of the results of the tests.
    """
    Error = DojoError

    def __init__(self, runmode=None, max_ncpus=1, max_level=None, verbose=0):
        """
        Args:
            runmode:
                `RunMode` instance specifying the options for parallel execution.
            max_ncpus:
                Max number of CPUs to use
            max_level:
                Max test level to perform.
            verbose:
                Verbosity level (int).
        """
        self.runmode = runmode if runmode else RunMode.sequential()
        self.max_ncpus = max_ncpus
        self.verbose = verbose

        # List of master classes that will be instanciated afterwards.
        # They are ordered according to the master level.
        classes = [m for m in DojoMaster.__subclasses__()]
        classes.sort(key=lambda cls: cls.dojo_level)

        self.master_classes = classes

        if max_level is not None:
            self.master_classes = classes[:max_level+1]

    def __str__(self):
        return repr_dojo_levels()

    def challenge_pseudo(self, pseudo, **kwargs):
        """
        This method represents the main entry point for client code.
        The Dojo receives a pseudo-like object and delegate the execution
        of the tests to the dojo_masters

        Args:
            `Pseudo` object or filename.
        """
        pseudo = Pseudo.aspseudo(pseudo)

        workdir = "DOJO_" + pseudo.name

        # Build master instances.
        masters = [cls(runmode=self.runmode, max_ncpus=self.max_ncpus,
                       verbose=self.verbose) for cls in self.master_classes]

        for master in masters:
            if master.accept_pseudo(pseudo, **kwargs):
                master.start_training(workdir, **kwargs)

################################################################################

class DojoMaster(object):
    """"
    Abstract base class for the dojo masters.
    Subclasses must define the class attribute level.
    """
    __metaclass__ = abc.ABCMeta

    Error = DojoError

    def __init__(self, runmode=None, max_ncpus=1, verbose=0):
        """
        Args:
            runmode:
                `RunMode` instance specifying the options for parallel execution.
            max_ncpus:
                Max number of CPUs to use
            verbose:
                Verbosity level (int).
        """
        self.runmode = runmode if runmode else RunMode.sequential()
        self.max_ncpus = max_ncpus
        self.verbose = verbose

        self.reports, self.errors = [], []

    @staticmethod
    def subclass_from_dojo_level(dojo_level):
        """Returns a subclass of `DojoMaster` given the dojo_level."""
        classes = []
        for cls in DojoMaster.__subclasses__():
            if cls.dojo_level == dojo_level:
                classes.append(cls)
        if len(classes) != 1:
            raise self.Error("Found %d masters with dojo_level %d" % (len(classes), dojo_level))

        return classes[0]

    def accept_pseudo(self, pseudo, **kwargs):
        """
        Returns True if the mast can train the pseudo.
        This method is called before testing the pseudo.

        A master can train the pseudo if his level == pseudo.dojo_level + 1
        """
        if not isinstance(pseudo, Pseudo):
            pseudo = Pseudo.from_filename(pseudo)

        ready = False
        if pseudo.dojo_level is None:
            # Hints are missing
            ready = (self.dojo_level == 0)
        else:
            if pseudo.dojo_level == self.dojo_level:
                # pseudo has already a test associated to this level.
                # check if it has the same accuracy.
                accuracy = kwargs.get("accuracy", "normal")
                if accuracy not in pseudo.dojo_report[self.dojo_key]:
                    ready = True
                else:
                    print("pseudo has already an entry for accuracy %s" % accuracy) 
                    ready = False

            else:
                # Pseudo level must be one less than the level of the master.
                ready = (pseudo.dojo_level == self.dojo_level - 1)

        if not ready:
            msg = "%s: Sorry, %s-san, I cannot train you" % (self.__class__.__name__, pseudo.name)
            print(msg)
        else:
            print("%s: Welcome %s-san, I'm your level-%d trainer" % (self.__class__.__name__, pseudo.name, self.dojo_level))
            self.pseudo = pseudo

        return ready

    @abc.abstractmethod
    def challenge(self, workdir, **kwargs):
        """Abstract method to run the calculation."""

    @abc.abstractmethod
    def make_report(self, **kwargs):
        """
        Abstract method.
        Returns report, isok.
            report:
                Dictionary with the results of the trial.
            isok:
                True if results are valid.
        """

    def write_dojo_report(self, report, overwrite_data=False, ignore_errors=False):
        """
        Write/update the DOJO_REPORT section of the pseudopotential.
        """
        dojo_key = self.dojo_key
        pseudo = self.pseudo

        # Read old_report from pseudo.
        old_report = pseudo.read_dojo_report()

        if dojo_key not in old_report:
            # Create new entry
            old_report[dojo_key] = {}
        else:
            # Check that we are not going to overwrite data.
            if self.accuracy in old_report[dojo_key] and not overwrite_data:
                raise self.Error("%s already exists in the old pseudo. Cannot overwrite data" % dojo_key)

        # Update old report card with the new one.
        old_report[dojo_key].update(report[dojo_key])

        # Write new report
        pseudo.write_dojo_report(old_report)

    def start_training(self, workdir, **kwargs):
        """Start the tests in the working directory workdir."""
        results = self.challenge(workdir, **kwargs)

        report, isok = self.make_report(results, **kwargs)

        json_pretty_dump(results, os.path.join(workdir, "report.json"))

        if isok:
            self.write_dojo_report(report)
        else:
            raise self.Error("isok: %s" % isok)

################################################################################


class HintsMaster(DojoMaster):
    """
    Level 0 master that analyzes the convergence of the total energy versus
    the plane-wave cutoff energy.
    """
    dojo_level = 0
    dojo_key = "hints"

    def challenge(self, workdir, **kwargs):
        pseudo = self.pseudo

        atols_mev = (10, 1, 0.1)
        factory = PPConvergenceFactory()

        workdir = os.path.join(workdir, "LEVEL_" + str(self.dojo_level))

        estep = kwargs.get("estep", 10)

        eslice = slice(5, None, estep)

        w = factory.work_for_pseudo(workdir, pseudo, eslice,
                                    runmode=self.runmode, atols_mev=atols_mev)

        if os.path.exists(w.workdir):
            shutil.rmtree(w.workdir)

        print("Converging %s in iterative mode with ecut_slice %s, ncpus = 1" %
              (pseudo.name, eslice))

        w.start()
        w.wait()

        wres = w.get_results()
        w.move("ITERATIVE")

        estart = max(wres["low"]["ecut"] - estep, 5)
        if estart <= 10:
            estart = 1 # To be sure we don't overestimate ecut_low

        estop, estep = wres["high"]["ecut"] + estep, 1

        erange = list(np.arange(estart, estop, estep))

        work = factory.work_for_pseudo(workdir, pseudo, erange,
                                       runmode=self.runmode,
                                       atols_mev=atols_mev)

        print("Finding optimal values for ecut in the interval %.1f %.1f %1.f, "
              "ncpus = %d" % (estart, estop, estep, self.max_ncpus))

        SimpleResourceManager(work, self.max_ncpus).run()

        wf_results = work.get_results()

        wf_results.json_dump(work.path_in_workdir("dojo_results.json"))

        return wf_results

    def make_report(self, results, **kwargs):
        d = {}
        for key in ["low", "normal", "high"]:
            d[key] = results[key]

        isok = not results.exceptions
        if not isok:
            d["_exceptions"] = str(results.exceptions)

        return {self.dojo_key: d}, isok

################################################################################


class DeltaFactorMaster(DojoMaster):
    """
    Level 1 master that drives the computation of the delta factor.
    """
    dojo_level = 1
    dojo_key = "delta_factor"

    def challenge(self, workdir, **kwargs):
        self.accuracy = kwargs.pop("accuracy", "normal")

        factory = DeltaFactory()

        workdir = os.path.join(workdir, "LEVEL_" + str(self.dojo_level))

        # 6750 is the value used in the deltafactor code.
        kppa = kwargs.get("kppa", 6750)
        kppa = 1

        if self.verbose:
            print("Running delta_factor calculation with %d python threads" % self.max_ncpus)
            print("Will use kppa = %d " % kppa)
            print("Runmode",self.runmode)

        work = factory.work_for_pseudo(workdir, self.runmode, self.pseudo, 
                                       accuracy=self.accuracy, kppa=kppa, ecut=None)

        retcodes = SimpleResourceManager(work, self.max_ncpus).run()

        if self.verbose:
            print("Returncodes %s" % retcodes)

        wf_results = work.get_results()

        wf_results.json_dump(work.path_in_workdir("dojo_results.json"))
        return wf_results

    def make_report(self, results, **kwargs):
        # Our results.
        v0, b0, bp = results["v0"], results["b0"], results["bp"]

        # Reference results (Wien2K).
        from pseudo_dojo.refdata.deltafactor import DeltaFactorDataset
        wien2k = DeltaFactorDataset().get_entry(self.pseudo.symbol)

        d = dict(
                v0=v0,
                b0=b0,
                bp=bp,
                rdelta_v0=100 * (v0 - wien2k.v0) / wien2k.v0,
                rdelta_b=100 * (b0 - wien2k.b0) / wien2k.b0,
                rdelta_bp=100 * (bp - wien2k.bp) / wien2k.bp,
            )

        isok = not results.exceptions 
        if not isok:
            d["_exceptions"] = str(results.exceptions)

        d = {self.accuracy: d}
        return {self.dojo_key: d}, isok

################################################################################

_key2level = {}
for cls in DojoMaster.__subclasses__():
    _key2level[cls.dojo_key] = cls.dojo_level


def dojo_key2level(key):
    """Return the trial level from the name found in the pseudo."""
    return _key2level[key]


def repr_dojo_levels():
    """String representation of the different levels of the Dojo."""
    level2key = {v: k for k,v in _key2level.items()}

    lines = ["Dojo level --> Challenge"]
    for k in sorted(level2key):
        lines.append("level %d --> %s" % (k, level2key[k]))
    return "\n".join(lines)

################################################################################
