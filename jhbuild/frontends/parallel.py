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
        self.check_cancel_interval = 1.0
        self.print_status_interval = 10.0
        self.num_worker = int(self.config.cmdline_options.num_worker)
        self.log_dir = self.config.cmdline_options.log_dir
        self.cancel = False

    def build(self, phases=None):

        # create tasks
        tasks = {}
        firstTasks = {}
        lastTasks = {}
        for module in self.module_list:
            build_phases = self.get_build_phases(module) if not phases else phases
            build_phases = filter(lambda phase: module.has_phase(phase), build_phases)
            prev = None
            skip = self.check_skip(module)
            for phase in build_phases:
                if phase == 'checkout' and hasattr(module, 'branch') and hasattr(module.branch, 'repomodule'):
                    key = (module.branch.repomodule, phase)
                else:
                    key = (module.name, phase)
                if key not in tasks:
                    tasks[key] = Task(key, module, phase)
                if prev is None:
                    firstTasks[module.name] = tasks[key]
                else:
                    tasks[key].dependencies.append(prev)
                if skip:
                    tasks[key].finished = True
                    tasks[key].success = True
                prev = tasks[key]
            if prev is not None:
                lastTasks[module.name] = prev

        for task in firstTasks.values():
            for dep in task.module.dependencies:
                if dep in lastTasks:
                    task.dependencies.append(lastTasks[dep])

        logging.info('created %d tasks from %d modules' % (len(tasks), len(self.module_list)))

        logging.info('starting workers')
        worker_available_cv = Condition()
        workers = [ Worker(worker_available_cv, self.config, self.module_list, self.module_set, self.log_dir) for i in range(self.num_worker)]
        for worker in workers:
            worker.start()

        unassignedTasks = [ task for task in tasks.values() ]
        cursor = 0

        with self.handle_signal(workers):
            while len(unassignedTasks) > 0:
                if self.cancel:
                    break
                cursor = (cursor + 1) % len(unassignedTasks)
                task = unassignedTasks[cursor]
                if not all([task2.finished for task2 in task.dependencies]):
                    continue
                if not all([task2.success for task2 in task.dependencies]):
                    task.finished = True
                    task.skip = True
                    unassignedTasks.pop(cursor)
                    continue
                logging.info('assigining task %s' % task)
                assigned = False
                while not assigned:
                    if self.cancel:
                        break
                    with worker_available_cv:
                        while not assigned:
                            if self.cancel:
                                break
                            for worker in workers:
                                if worker.set_task(task):
                                    logging.info('assigned task %s to %s' % (task, worker))
                                    assigned = True
                                    unassignedTasks.pop(cursor)
                                    break
                            worker_available_cv.wait(1.0)

        logging.info('stopping build')
        for worker in workers:
            worker.set_cancel()
        for worker in workers:
            worker.join()
        logging.info('finished build')

        success = True
        for task in tasks.values():
            if (task.finished) and (not task.skip) and (not task.success):
                logging.warn('task %s failed.' % task)
                success = False
        if success:
            logging.info('no build failed')

    @contextmanager
    def handle_signal(self, workers):
        signals = [signal.SIGINT, signal.SIGTERM]
        prev_handlers = {
            s: signal.getsignal(s)
            for s in signals
        }
        def handler(signum, stack):
            self.cancel = True
            for worker in workers:
                worker.set_cancel()
        for s in signals:
            signal.signal(s, handler)
        yield
        for s in signals:
            signal.signal(s, prev_handlers[s])

    def check_skip(self, module):
        if self.config.min_age is not None:
            installdate = eslf.module_set.packagedb.installdate(module['name'])
            if installdate > self.config.min_age:
                return True
        return False

class ParallelBuildScriptProxy(BuildScript):

    def __init__(self, config, module_list, module_set, is_cancel_fn, log_file):
        # BuildScript is not derived from object.
        BuildScript.__init__(self, config, module_list, module_set)
        self.__is_cancel_fn = is_cancel_fn
        self.__log_file = log_file

    def set_action(self, action, module, module_num=-1, action_target=None):
        self.message('set_action: action = %s' % action)

    def execute(self, command, hint=None, cwd=None, extra_env=None):
        if not command:
            raise CommandError(_('No command given'))

        kws = {
            'close_fds': True,
            'preexec_fn': os.setsid,
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
        try:
            if self.__log_file is not None:
                with open(self.__log_file, 'w') as f:
                    kws['stdout'] = f
                    p = subprocess.Popen(command, **kws)
            else:
                p = subprocess.Popen(command, **kws)
        except OSError as e:
            raise CommandError(str(e))

        code = self.process_popen(p)
        if code > 0:
            raise CommandError('Command exited with %d' % code)
        #if code < 0:
        #    raise CommandError('Command is interrupted by signal %d' % (-code))

    def process_popen(self, popen):
        read_set = []
        if popen.stdout:
            read_set.append(popen.stdout)
        if popen.stderr:
            read_set.append(popen.stderr)
        while read_set:
            if self.__is_cancel_fn() and popen.poll() == None:
                os.killpg(popen.pid, signal.SIGTERM)
            rlist, wlist, xlist = select.select(read_set, [], [], 0.5)
            for fd in rlist:
                line = fd.readline()
                if len(line) == 0:
                    read_set.remove(fd)
                else:
                    self.message(line)
        return popen.wait()

    def message(self, msg, module_num=-1):
        '''Display a message to the user'''
        print(msg)

class Task(object):

    def __init__(self, key, module, phase):
        self.key = key
        self.module = module
        self.phase = phase
        self.dependencies = []
        self.finished = False
        self.success = None
        self.skip = None
        self.error = None

    def __str__(self):
        return '<Task %s:%s>' % self.key

class Worker(Thread):

    def __init__(self, notify_available_cv, config, module_list, module_set, log_dir):
        super(Worker, self).__init__(target=self.__run)
        self.__task = None
        self.__cv = Condition()
        self.__cancel = False
        self.__notify_available_cv = notify_available_cv
        self.__config = config
        self.__module_list = module_list
        self.__module_set = module_set
        self.__log_dir = log_dir

    def set_task(self, task):
        with self.__cv:
            if self.__task is not None:
                return False
            self.__task = task
            self.__cv.notify_all()
        return True

    def set_cancel(self):
        with self.__cv:
            self.__cancel = True
            self.__cv.notify_all()

    def is_cancel(self):
        with self.__cv:
            return self.__cancel

    def __run(self):
        while not self.__cancel:
            with self.__cv:
                while self.__task is None:
                    if self.__cancel:
                        break
                    self.__cv.wait(1)
                task = self.__task
            if self.__cancel:
                break

            file_name = ('%s_%s.log' % task.key).replace('/', '_')
            log_file = os.path.join(self.__log_dir, file_name) if self.__log_dir is not None else None
            proxy = ParallelBuildScriptProxy(self.__config, self.__module_list, self.__module_set, self.is_cancel, log_file)
            try:
                error, altphases = task.module.run_phase(proxy, task.phase)
            except Exception as e:
                error = str(e)
            task.finished = True
            task.success = not bool(error)
            task.error = error
            with self.__cv:
                self.__task = None
            with self.__notify_available_cv:
                self.__notify_available_cv.notify_all()

class Queue(object):

    def __init__(self, items):
        self.__items = items
        self.__cv = Condition()

    def pop(self):
        with self.__cv:
            while len(self.__items) == 0:
                self.__cv.wait()
            return self.__items.pop(0)

    def push(self, item):
        with self.__cv:
            self.__items.append(item)
            self.__cv.notify()
