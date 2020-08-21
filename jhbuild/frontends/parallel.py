import os
import logging
import subprocess
import sys
import select
from threading import Thread
from threading import Event
from threading import Lock
from threading import Condition
from datetime import datetime

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
                'finished': Event(),
                'log': '',
                'logger': None,
            }
            for module in self.module_list
        }

        self.cancel = False
        self.cancel_lock = Lock()
        self.cancel_first_module = None

        for module in self.module_list:
            self.modules[module.name]['dependencies'] = [self.modules[dependency] for dependency in module.dependencies if dependency in self.modules]

        for module in self.module_list:
            def Logger(name):
                def wrapper(content):
                    self.modules[name]['log'] += datetime.now().isoformat() + ' ' + content + '\n'
                return wrapper
            self.modules[module.name]['logger'] = Logger(module.name)

        self.worker_queue = Queue(range(1)) # TODO

    def __set_cancel(self, module, cancel):
        with self.cancel_lock:
            if (not self.cancel) and cancel:
                self.cancel_first_module = module
            self.cancel = self.cancel or cancel

    def build(self, phases=None):
        try:
            logging.info('creating workers')
            for module in self.module_list:
                self.modules[module.name]['worker'] = Thread(target=self.build_one, args=[phases, self.modules[module.name]])
            logging.info('start workers')
            for module in self.module_list:
                self.modules[module.name]['worker'].start()
            for module in self.module_list:
                while True:
                    try:
                        self.modules[module.name]['worker'].join()
                        break
                    except Exception as e:
                        logging.error(e)
                logging.info('confirmed module %s finished.', self.modules[module.name])
        finally:
            if not all(map(lambda x: x['success'], self.modules.values())):
                total = len(self.modules)
                success = sum(map(lambda x: int(x['success'] == True), self.modules.values()))
                fail = total - success
                logging.info('Not all modules succeeded. total = %d, success = %d, fail = %d', total, success, fail)
                logging.info('First failed module = %s', self.cancel_first_module['name'])
        return 0

    def build_one(self, phases, module):
        logger = module['logger']
        logger('start')
        try:
            for dependency in module['dependencies']:
                dependency['finished'].wait()

            worker = self.worker_queue.pop()
            cancel = True
            try:
                if self.cancel:
                    return

                if self.check_skip(module):
                    return

                if not phases:
                    build_phases = self.get_build_phases(module['instance'])
                else:
                    build_phases = phases

                for build_phase in build_phases:
                    logger('%s %s' %(module['name'], build_phase))

                    # Module will call back BuildScript's method like execute, but it does not pass current module.
                    proxy = ParallelBuildScriptProxy(self.config, self.module_list, self.module_set, self.modules, module)
                    try:
                        error, altphases = module['instance'].run_phase(proxy, build_phase)
                    except SkipToEnd:
                        logger('skiped')
                        error = False
                    if error:
                        module['success'] = False
                        logger('error = %s' % error)
                        return
                cancel = False
            finally:
                self.__set_cancel(module, cancel)
                self.worker_queue.push(worker)
        except Exception as e:
            logging.exception(e)
            logger('error = %s' % str(e))
        finally:
            logger('finished')
            module['finished'].set()

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

    def __init__(self, config, module_list, module_set, modules, module):
        # BuildScript is not derived from object.
        BuildScript.__init__(self, config, module_list, module_set)
        self.modules = modules
        self.module = module

    def set_action(self, action, module, module_num=-1, action_target=None):
        self.modules[module.name]['logger']('set_action: action = %s' % action)

    def execute(self, command, hint=None, cwd=None, extra_env=None):
        log = self.module['logger']
        if not command:
            raise CommandError(_('No command given'))

        kws = {
            'close_fds': True
            }
        print_args = {'cwd': ''}
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
                    log(self.config.print_command_pattern % print_args)
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

        self.process_popen(p)

    def process_popen(self, popen):
        read_set = []
        if popen.stdout:
            read_set.append(popen.stdout)
        if popen.stderr:
            read_set.append(popen.stderr)
        try:
            while read_set:
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
                os.kill(popen.pid, SIGINT)
            except OSError:
                # process might already be dead.
                pass
        return popen.wait()

    def message(self, msg, module_num=-1):
        '''Display a message to the user'''
        log = self.module['logger']
        log(msg)

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
