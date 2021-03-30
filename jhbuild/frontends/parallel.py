import os
import logging
import subprocess
import sys
import select
import signal
import traceback

from threading import Thread
from threading import Event
from threading import Lock
from threading import Condition
from datetime import datetime
from time import time
from time import sleep
from contextlib import contextmanager

from jhbuild.frontends.buildscript import BuildScript
from jhbuild.errors import CommandError, FatalError, SkipToEnd

class ParallelBuildScript(BuildScript):

    def __init__(self, config, module_list=None, module_set=None):
        # BuildScript is not derived from object.
        BuildScript.__init__(self, config, module_list, module_set)
        self.module_list = module_list
        self.module_set = module_set
        self.num_worker = int(self.config.cmdline_options.num_worker)
        self.log_dir = self.config.cmdline_options.log_dir
        self.cancel = False

    def build(self, phases=None):

        try:
            os.makedirs(self.log_dir)
        except:
            pass

        # create tasks
        tasks = {}
        module_first_tasks = {}
        module_last_tasks = {}
        module_phases = {}

        for module in self.module_list:
            build_phases = self.get_build_phases(module) if not phases else phases
            build_phases = filter(lambda phase: module.has_phase(phase), build_phases)
            prev = None
            skip = self.check_skip(module)
            for index, phase in enumerate(build_phases):
                try:
                    skip_phase = module.skip_phase(self, phase, build_phases[index - 1] if index > 0 else None)
                except SkipToEnd:
                    skip = True

                if phase == 'checkout' and hasattr(module, 'branch') and hasattr(module.branch, 'repomodule'):
                    key = (module.branch.repomodule, phase)
                else:
                    key = (module.name, phase)
                if key not in tasks:
                    tasks[key] = Task(key, module, phase)

                if module.name not in module_phases:
                    module_phases[module.name] = []
                for prev_phase in module_phases[module.name]:
                    prev_phase.next_phases.append(tasks[key])
                module_phases[module.name].append(tasks[key])

                if prev is None:
                    module_first_tasks[module.name] = tasks[key]
                else:
                    tasks[key].dependencies.append(prev)

                if skip or skip_phase:
                    tasks[key].skip = True
                prev = tasks[key]
            if prev is not None:
                module_last_tasks[module.name] = prev

        for task in module_first_tasks.values():
            for dep in task.module.dependencies:
                if dep in module_last_tasks:
                    task.dependencies.append(module_last_tasks[dep])

        logging.info('created %d tasks from %d modules' % (len(tasks), len(self.module_list)))

        logging.info('starting workers')

        num_tasks = len(tasks)
        task_queue = Queue([])
        for task in tasks.itervalues():
            if task.finished:
                continue
            if len(task.dependencies) == 0:
                task.queued = True
                task_queue.push(task)

        workers = [
            Worker(
                i,
                task_queue,
                self.config,
                self.module_list,
                self.module_set,
                self.log_dir,
            )
            for i in range(self.num_worker)
        ]

        for worker in workers:
            worker.start()

#         def RunIPython(tasks, workers, task_queue):
#             import IPython; IPython.embed()

#         ipython = Thread(target=RunIPython, args=[tasks, workers, task_queue])
#         ipython.start()

        with self.handle_signal(task_queue):
            while True:
                if task_queue.is_cancel():
                    break
                if all([task.finished for task in tasks.itervalues()]):
                    break
                for task in tasks.itervalues():
                    if task.queued:
                        continue
                    if all([dep.finished for dep in task.dependencies]):
                        task.queued = True
                        task_queue.push(task)
                sleep(0.1)

        logging.info('stopping workers')
        task_queue.cancel()
        for worker in workers:
            worker.join()
        logging.info('stopped workers')

        success = True
        for task in tasks.values():
            if (task.finished) and (not task.skip) and (not task.success):
                logging.warn('task %s failed. error = %s' % (task, task.error))
                success = False
        if success:
            logging.info('no build failed')
            return 0
        else:
            return 1

    def get_task(self, tasks, worker_cv):
        logging.info('getting task')
        while True:
            if self.cancel:
                return
            if len(tasks) == 0:
                return
            for key, task in tasks.items():
                if len(task.dependencies) == 0 or all([task2.finished for task2 in task.dependencies]):
                    if not all([task2.success for task2 in task.dependencies]):
                        del tasks[key]
                        continue
                    logging.info('got task %s' % task)
                    return task
            with worker_cv:
                worker_cv.wait(1.0)

    def assign_task(self, tasks, task, workers, worker_cv):
        logging.info('assigining task %s' % task)
        while True:
            if self.cancel:
                break
            for worker in workers:
                if worker.set_task(task):
                    logging.info('assigned task %s to %s' % (task, worker))
                    del tasks[task.key]
                    return
            with worker_cv:
                worker_cv.wait(1.0)

    def check_skip(self, module):
        if self.config.min_age is not None:
            installdate = self.module_set.packagedb.installdate(module['name'])
            if installdate > self.config.min_age:
                return True
        return False

    @contextmanager
    def handle_signal(self, task_queue):
        signals = [signal.SIGINT, signal.SIGTERM]
        prev_handlers = {
            s: signal.getsignal(s)
            for s in signals
        }
        def handler(signum, stack):
            self.cancel = True
            task_queue.cancel()
            #if signum in prev_handlers:
            #    if callable(prev_handlers[signum]):
            #        prev_handlers[signum]()

        for sig in signals:
            signal.signal(sig, handler)
        yield
        for sig in signals:
            signal.signal(sig, prev_handlers[sig])

    def message(self, msg, module_num=-1):
        '''Display a message to the user'''
        print(msg)

class ParallelBuildScriptProxy(BuildScript):

    def __init__(self, config, module_list, module_set, log_file):
        # BuildScript is not derived from object.
        BuildScript.__init__(self, config, module_list, module_set)
        self.__log_file = log_file
        self.__log_file_fd = open(self.__log_file, 'w') if log_file is not None else None

    def finalize(self):
        if self.__log_fild_fd is not None:
            os.close(self.__log_fild_fd)
            self.__log_fild_fd = None

    def set_action(self, action, module, module_num=-1, action_target=None):
        self.message('set_action: action = %s' % action)

    def execute(self, command, hint=None, cwd=None, extra_env=None):
        if not command:
            raise CommandError(_('No command given'))

        kws = {
            'close_fds': True,
        #    'preexec_fn': os.setsid,
        }

        print_args = {
            'cwd': ''
        }

        if cwd:
            print_args['cwd'] = cwd
        else:
            try:
                print_args['cwd'] = os.getcwd()
            except OSError:
                pass

        if isinstance(command, (str, unicode)):
            kws['shell'] = True
            print_args['command'] = command
        else:
            print_args['command'] = ' '.join(command)

        # get rid of hint if pretty printing is disabled.
        if not self.config.pretty_print:
            hint = None
        elif os.name == 'nt':
            # pretty print also doesn't work on Windows;
            # see https://bugzilla.gnome.org/show_bug.cgi?id=670349 
            hint = None

        if not self.config.quiet_mode:
            if self.config.print_command_pattern:
                try:
                    self.message(self.config.print_command_pattern % print_args)
                except TypeError as e:
                    raise FatalError('\'print_command_pattern\' %s' % e)
                except KeyError as e:
                    raise FatalError(_('%(configuration_variable)s invalid key'
                                       ' %(key)s' % \
                                       {'configuration_variable' :
                                            '\'print_command_pattern\'',
                                        'key' : e}))


        if cwd is not None:
            kws['cwd'] = cwd

        if extra_env is not None:
            kws['env'] = os.environ.copy()
            kws['env'].update(extra_env)

        kws['stderr'] = subprocess.STDOUT
        command = self._prepare_execute(command)
        if self.__log_file is not None:
            kws['stdout'] = self.__log_file_fd
            subprocess.check_call(command, **kws)
        else:
            subprocess.check_call(command, **kws)

    def message(self, msg, module_num=-1):
        '''Display a message to the user'''
        if self.__log_file_fd is not None:
            self.__log_file_fd.write(msg + '\n')
        else:
            print(msg)

class Task(object):

    def __init__(self, key, module, phase):
        self.key = key
        self.module = module
        self.phase = phase
        self.next_phases = []
        self.dependencies = []
        self.skip = None
        self.queued = False
        self.success = None
        self.finished = None
        self.error = None

    def __str__(self):
        return '<Task %s:%s>' % self.key

class Worker(Thread):

    def __init__(self, worker_index, task_queue, config, module_list, module_set, log_dir):
        super(Worker, self).__init__(target=self.__run)
        self.__worker_index = worker_index
        self.__task_queue = task_queue

        self.__config = config
        self.__module_list = module_list
        self.__module_set = module_set
        self.__log_dir = log_dir

    def __run(self):
        self.log('start')
        while True:
            task = self.__task_queue.pop()
            if task is None:
                break

            assert all(map(lambda dep: dep.finished, task.dependencies))

            if any(map(lambda dep: not dep.success, task.dependencies)):
                task.skip = True 
                self.log('failure in dependency. mark task %s as skipped.', task)

            if not task.skip:
                self.log('starting task %s', task)
                file_name = ('%s_%s.log' % task.key).replace('/', '_')
                log_file = os.path.join(self.__log_dir, file_name) if self.__log_dir is not None else None
                proxy = ParallelBuildScriptProxy(self.__config, self.__module_list, self.__module_set, log_file)
                try:
                    error, altphases = task.module.run_phase(proxy, task.phase)
                except SkipToEnd:
                    task.skip = True
                    for next_phase in task.next_phases:
                        next_phase.skip = True
                    error = ''
                except Exception as e:
                    error = str(e)
                except:
                    error = 'unknown error'
            else:
                error = ''

            task.finished = True
            task.success = not bool(error)
            task.error = error
            if task.skip:
                self.log('skipped task %s', task)
            elif task.success:
                self.log('finished task %s', task)
            else:
                self.log('failed task %s: %s', task, task.error)
        self.log('stop')

    def log(self, fmt, *args):
        print(('[Worker=%d] ' % self.__worker_index) + fmt % args)

class Queue(object):

    def __init__(self, items):
        self.__items = items
        self.__cv = Condition()
        self.__cancel = False

    def pop(self):
        with self.__cv:
            while len(self.__items) == 0 and self.__cancel == False:
                self.__cv.wait(0.5)
            if self.__cancel:
                return None
            else:
                return self.__items.pop(0)

    def push(self, item):
        with self.__cv:
            self.__items.append(item)
            self.__cv.notify()

    def size(self):
        with self.__cv:
            return len(self.__items)

    def cancel(self):
        with self.__cv:
            self.__cancel = True
            self.__cv.notify()

    def is_cancel(self):
        return self.__cancel
