# jhbuild - a build script for GNOME 1.x and 2.x
# Copyright (C) 2001-2006  James Henstridge
# Copyright (C) 2003-2004  Seth Nickell
#
#   buildscript.py: base class of the various interface types
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

import os

from jhbuild.utils import packagedb
from jhbuild.errors import FatalError, CommandError

class BuildScript:
    def __init__(self, config, module_list):
        if self.__class__ is BuildScript:
            raise NotImplementedError('BuildScript is an abstract base class')

        self.modulelist = module_list
        self.module_num = 0

        self.config = config

        if not os.path.exists(self.config.checkoutroot):
            try:
                os.makedirs(self.config.checkoutroot)
            except OSError:
                raise FatalError(_('checkout root can not be created'))
        if not os.access(self.config.checkoutroot, os.R_OK|os.W_OK|os.X_OK):
            raise FatalError(_('checkout root must be writable'))
        if not os.path.exists(self.config.prefix):
            try:
                os.makedirs(self.config.prefix)
            except OSError:
                raise FatalError(_('install prefix can not be created'))
        if not os.access(self.config.prefix, os.R_OK|os.W_OK|os.X_OK):
            raise FatalError(_('install prefix must be writable'))

        packagedbdir = os.path.join(self.config.prefix, 'share', 'jhbuild')
        try:
            if not os.path.isdir(packagedbdir):
                os.makedirs(packagedbdir)
        except OSError:
            raise FatalError(_('could not create directory %s') % packagedbdir)
        self.packagedb = packagedb.PackageDB(os.path.join(packagedbdir,
                                                          'packagedb.xml'))

    def execute(self, command, hint=None, cwd=None, extra_env=None):
        '''Executes the given command.

        If an error occurs, CommandError is raised.  The hint argument
        gives a hint about the type of output to expect.
        '''
        raise NotImplementedError

    def start_clean(self):
        '''Hook to perform actions at start of clean.'''
        pass

    def end_clean(self):
        '''Hook to perform actions at end of clean.'''
        pass

    def clean(self):
        self.start_clean()

        for module in self.modulelist:
            try:
                module.do_clean(self)
            except CommandError:
                self.message(_('Failed to clean %s') % module.name)

        self.end_clean()
        return 0


    def build(self):
        '''start the build of the current configuration'''
        self.start_build()
        
        failures = [] # list of modules that couldn't be built
        self.module_num = 0
        for module in self.modulelist:
            self.module_num = self.module_num + 1

            if self.config.min_time is not None:
                installdate = self.packagedb.installdate(module.name)
                if installdate > self.config.min_time:
                    self.message(_('Skipping %s (installed recently)') % module.name)
                    continue

            self.start_module(module.name)
            failed = False
            for dep in module.dependencies:
                if dep in failures:
                    if self.config.nopoison:
                        self.message(_('module %(mod)s will be build even though %(dep)s failed')
                                     % { 'mod':module.name, 'dep':dep })
                    else:
                        self.message(_('module %(mod)s not built due to non buildable %(dep)s')
                                     % { 'mod':module.name, 'dep':dep })
                        failed = True
            if failed:
                failures.append(module.name)
                self.end_module(module.name, failed)
                continue

            state = module.STATE_START
            while state != module.STATE_DONE:
                self.start_phase(module.name, state)
                nextstate, error, altstates = module.run_state(self, state)
                self.end_phase(module.name, state, error)

                if error:
                    newstate = self.handle_error(module, state,
                                                 nextstate, error,
                                                 altstates)
                    if newstate == 'fail':
                        failures.append(module.name)
                        failed = True
                        state = module.STATE_DONE
                    else:
                        state = newstate
                else:
                    state = nextstate
            self.end_module(module.name, failed)
        self.end_build(failures)
        if failures:
            return 1
        return 0

    def start_build(self):
        '''Hook to perform actions at start of build.'''
        pass
    def end_build(self, failures):
        '''Hook to perform actions at end of build.
        The argument is a list of modules that were not buildable.'''
        pass
    def start_module(self, module):
        '''Hook to perform actions before starting a build of a module.'''
        pass
    def end_module(self, module, failed):
        '''Hook to perform actions after finishing a build of a module.
        The argument is true if the module failed to build.'''
        pass
    def start_phase(self, module, state):
        '''Hook to perform actions before starting a particular build phase.'''
        pass
    def end_phase(self, module, state, error):
        '''Hook to perform actions after finishing a particular build phase.
        The argument is a string containing the error text if something
        went wrong.'''
        pass

    def message(self, msg, module_num=-1):
        '''Display a message to the user'''
        raise NotImplementedError

    def set_action(self, action, module, module_num=-1, action_target=None):
        '''inform the buildscript of a new stage of the build'''
        raise NotImplementedError

    def handle_error(self, module, state, nextstate, error, altstates):
        '''handle error during build'''
        raise NotImplementedError
