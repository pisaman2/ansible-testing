# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# Make coding more python3-ish
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

from ansible.compat.six.moves import queue

import json
import multiprocessing
import os
import signal
import sys
import time
import traceback
import zlib

from jinja2.exceptions import TemplateNotFound

# TODO: not needed if we use the cryptography library with its default RNG
# engine
HAS_ATFORK=True
try:
    from Crypto.Random import atfork
except ImportError:
    HAS_ATFORK=False

from ansible.errors import AnsibleError, AnsibleConnectionFailure
from ansible.executor.task_executor import TaskExecutor
from ansible.executor.task_result import TaskResult
from ansible.playbook.handler import Handler
from ansible.playbook.task import Task
from ansible.vars.unsafe_proxy import AnsibleJSONUnsafeDecoder

from ansible.utils.debug import debug

__all__ = ['WorkerProcess']


class WorkerProcess(multiprocessing.Process):
    '''
    The worker thread class, which uses TaskExecutor to run tasks
    read from a job queue and pushes results into a results queue
    for reading later.
    '''

    def __init__(self, tqm, main_q, rslt_q, hostvars_manager, loader):

        super(WorkerProcess, self).__init__()
        # takes a task queue manager as the sole param:
        self._main_q   = main_q
        self._rslt_q   = rslt_q
        self._hostvars = hostvars_manager
        self._loader   = loader

        # dupe stdin, if we have one
        self._new_stdin = sys.stdin
        try:
            fileno = sys.stdin.fileno()
            if fileno is not None:
                try:
                    self._new_stdin = os.fdopen(os.dup(fileno))
                except OSError:
                    # couldn't dupe stdin, most likely because it's
                    # not a valid file descriptor, so we just rely on
                    # using the one that was passed in
                    pass
        except ValueError:
            # couldn't get stdin's fileno, so we just carry on
            pass

    def run(self):
        '''
        Called when the process is started, and loops indefinitely
        until an error is encountered (typically an IOerror from the
        queue pipe being disconnected). During the loop, we attempt
        to pull tasks off the job queue and run them, pushing the result
        onto the results queue. We also remove the host from the blocked
        hosts list, to signify that they are ready for their next task.
        '''

        if HAS_ATFORK:
            atfork()

        while True:
            task = None
            try:
                #debug("waiting for work")
                (host, task, basedir, zip_vars, compressed_vars, play_context, shared_loader_obj) = self._main_q.get(block=False)

                if compressed_vars:
                    job_vars = json.loads(zlib.decompress(zip_vars))
                else:
                    job_vars = zip_vars

                job_vars['hostvars'] = self._hostvars.hostvars()

                debug("there's work to be done! got a task/handler to work on: %s" % task)

                # because the task queue manager starts workers (forks) before the
                # playbook is loaded, set the basedir of the loader inherted by
                # this fork now so that we can find files correctly
                self._loader.set_basedir(basedir)

                # Serializing/deserializing tasks does not preserve the loader attribute,
                # since it is passed to the worker during the forking of the process and
                # would be wasteful to serialize. So we set it here on the task now, and
                # the task handles updating parent/child objects as needed.
                task.set_loader(self._loader)

                # execute the task and build a TaskResult from the result
                debug("running TaskExecutor() for %s/%s" % (host, task))
                executor_result = TaskExecutor(
                    host,
                    task,
                    job_vars,
                    play_context,
                    self._new_stdin,
                    self._loader,
                    shared_loader_obj,
                ).run()
                debug("done running TaskExecutor() for %s/%s" % (host, task))
                task_result = TaskResult(host, task, executor_result)

                # put the result on the result queue
                debug("sending task result")
                self._rslt_q.put(task_result)
                debug("done sending task result")

            except queue.Empty:
                time.sleep(0.0001)
            except AnsibleConnectionFailure:
                try:
                    if task:
                        task_result = TaskResult(host, task, dict(unreachable=True))
                        self._rslt_q.put(task_result, block=False)
                except:
                    break
            except Exception as e:
                if isinstance(e, (IOError, EOFError, KeyboardInterrupt)) and not isinstance(e, TemplateNotFound):
                    break
                else:
                    try:
                        if task:
                            task_result = TaskResult(host, task, dict(failed=True, exception=traceback.format_exc(), stdout=''))
                            self._rslt_q.put(task_result, block=False)
                    except:
                        debug("WORKER EXCEPTION: %s" % e)
                        debug("WORKER EXCEPTION: %s" % traceback.format_exc())
                        break

        debug("WORKER PROCESS EXITING")


