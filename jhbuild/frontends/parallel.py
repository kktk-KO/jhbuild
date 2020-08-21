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
        self.modules = {
            module.name: {
                'name': module.name,
                'instance': module,
                'dependencies': None,
                'worker': None,
                'success': False,
                'fail': False,
                'skipped': False,
                'finished': Event(),
                'log': '',
                'logger': None,
            }
            for module in self.module_list
        }

        self.cancel = {'value': False}

        for module in self.module_list:
            self.modules[module.name]['dependencies'] = [self.modules[dependency] for dependency in module.dependencies if dependency in self.modules]

        for module in self.module_list:
            def Logger(name):
                def wrapper(content):
                    self.modules[name]['log'] += datetime.now().isoformat() + ' ' + content + '\n'
                return wrapper
            self.modules[module.name]['logger'] = Logger(module.name)

        self.worker_queue = Queue(range(1)) # TODO

        self.check_cancel_interval = 1.0
        self.print_status_interval = 10.0

    def build(self, phases=None):
        logging.info('creating workers')
        for module in self.module_list:
            self.modules[module.name]['worker'] = Thread(target=self.build_one, args=[phases, self.modules[module.name]])
        logging.info('start workers')

        cancel_detected = False
        last_status_time = 0
        with self.handle_signal():
            for module in self.module_list:
                self.modules[module.name]['worker'].start()
            while not self.check_build_finish():
                if self.cancel['value'] and (not cancel_detected):
                    logging.info('cancel is reqested.')
                    cancel_detected = True
                if time() - last_status_time > self.print_status_interval:
                    last_status_time = time()
                    total, success, skipped, fail, unfinished = self.get_build_status()
                    logging.info('total = %d, success = %d, skipped = %d, fail = %d, unfinished = %d', total, success, skipped, fail, unfinished)
                sleep(self.check_cancel_interval)
            for module in self.module_list:
                self.modules[module.name]['worker'].join()
        total, success, skipped, fail, unfinished = self.get_build_status()
        logging.info('total = %d, success = %d, skipped = %d, fail = %d, unfinished = %d', total, success, skipped, fail, unfinished)
        return total == success + skipped

    def check_build_finish(self):
        return all([not module['worker'].is_alive() for module in self.modules.values()])

    def get_build_status(self):
        total = len(self.modules)
        success = sum(map(lambda x: int(x['success'] == True), self.modules.values()))
        skipped = sum(map(lambda x: int(x['skipped'] == True), self.modules.values()))
        fail = sum(map(lambda x: int(x['fail'] == True), self.modules.values()))
        unfinished = total - (success + skipped + fail)
        return total, success, skipped, fail, unfinished

    @contextmanager
    def handle_signal(self):
        prev_handler = signal.getsignal(signal.SIGINT)
        def handler(signum, stack):
            self.cancel['value'] = True
        signal.signal(signal.SIGINT, handler)
        yield
        signal.signal(signal.SIGINT, prev_handler)

    def build_one(self, phases, module):
        worker = None
        logger = module['logger']
        logger('start')
        try:
            for dependency in module['dependencies']:
                dependency['finished'].wait()

            worker = self.worker_queue.pop()
            if self.cancel['value']:
                module['success'] = False
                module['fail'] = False
                module['skipped'] = True
                return

            if self.check_skip(module):
                module['success'] = False
                module['fail'] = False
                module['skipped'] = True
                return

            if not phases:
                build_phases = self.get_build_phases(module['instance'])
            else:
                build_phases = phases

            for build_phase in build_phases:
                logger('%s %s' %(module['name'], build_phase))

                # Module will call back BuildScript's method like execute, but it does not pass current module.
                proxy = ParallelBuildScriptProxy(self.config, self.module_list, self.module_set, self.modules, module, self.cancel)
                try:
                    error, altphases = module['instance'].run_phase(proxy, build_phase)
                except SkipToEnd:
                    logger('skiped')
                    error = False
                if error:
                    raise Exception(error)
            module['success'] = True
            module['fail'] = False
            module['skipped'] = False
        except Exception as e:
            for line in traceback.format_exc().split('\n'):
                logger(line)
            module['success'] = False
            module['fail'] = True
            module['skipped'] = False
            self.cancel['value'] = True
        finally:
            logger('finished')
            module['finished'].set()
            if worker is not None:
                self.worker_queue.push(worker)

    def check_skip(self, module):
        logger = module['logger']
        if self.config.min_age is not None:
            installdate = eslf.module_set.packagedb.installdate(module['name'])
            if installdate > self.config.min_age:
                logger('Installed recently. skip.')
                return True
        logger('Not installed recently. continue.')
        return False

class ParallelBuildScriptProxy(BuildScript):

    def __init__(self, config, module_list, module_set, modules, module, cancel):
        # BuildScript is not derived from object.
        BuildScript.__init__(self, config, module_list, module_set)
        self.modules = modules
        self.module = module
        self.cancel = cancel

    def set_action(self, action, module, module_num=-1, action_target=None):
        self.modules[module.name]['logger']('set_action: action = %s' % action)

    def execute(self, command, hint=None, cwd=None, extra_env=None):
        logger = self.module['logger']
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
                    logger(self.config.print_command_pattern % print_args)
                except TypeError as e:
                    raise FatalError('\'print_command_pattern\' %s' % e)
                except KeyError as e:
                    raise FatalError(_('%(configuration_variable)s invalid key'
                                       ' %(key)s' % \
                                       {'configuration_variable' :
                                            '\'print_command_pattern\'',
                                        'key' : e}))

        kws['stdout'] = subprocess.PIPE
        kws['stderr'] = subprocess.PIPE

        if cwd is not None:
            kws['cwd'] = cwd

        if extra_env is not None:
            kws['env'] = os.environ.copy()
            kws['env'].update(extra_env)

        command = self._prepare_execute(command)

        try:
            p = subprocess.Popen(command, **kws)
        except OSError as e:
            raise CommandError(str(e))

        return self.process_popen(p)

    def process_popen(self, popen):
        logger = self.module['logger']
        read_set = []
        if popen.stdout:
            read_set.append(popen.stdout)
        if popen.stderr:
            read_set.append(popen.stderr)
        try:
            while read_set:
                if self.cancel['value']:
                    logger('got cancel request.')
                    os.kill(popen.pid, signal.SIGINT)
                    break
                rlist, wlist, xlist = select.select(read_set, [], [])
                for fd in rlist:
                    line = fd.readline()
                    if len(line) == 0:
                        read_set.remove(fd)
                    else:
                        self.module['log'] += line
        except KeyboardInterrupt:
            # interrupt received.  Send SIGINT to child process.
            try:
                os.kill(popen.pid, signal.SIGINT)
            except OSError:
                # process might already be dead.
                pass
        return popen.wait()

    def message(self, msg, module_num=-1):
        '''Display a message to the user'''
        logger = self.module['logger']
        logger(msg)

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
