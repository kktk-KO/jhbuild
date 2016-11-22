# jhbuild - a tool to ease building collections of source packages
# Copyright (C) 2001-2006  James Henstridge
#
#   cmake.py: cmake module type definitions.
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

__metaclass__ = type

import os

from jhbuild.errors import BuildStateError, CommandError
from jhbuild.modtypes import \
     Package, DownloadableModule, register_module_type, MakeModule
from jhbuild.modtypes.autotools import collect_args
from jhbuild.commands.sanitycheck import inpath

__all__ = [ 'CMakeModule' ]

class CMakeModule(MakeModule, DownloadableModule):
    """Base type for modules that use CMake build system."""
    type = 'cmake'

    PHASE_CHECKOUT = DownloadableModule.PHASE_CHECKOUT
    PHASE_FORCE_CHECKOUT = DownloadableModule.PHASE_FORCE_CHECKOUT
    PHASE_CONFIGURE = 'configure'
    PHASE_BUILD = 'build'
    PHASE_DIST = 'dist'
    PHASE_INSTALL = 'install'

    def __init__(self, name, branch=None,
                 cmakeargs='', makeargs='',
                 skip_install_phase=False):
        MakeModule.__init__(self, name, branch=branch, makeargs=makeargs)
        self.cmakeargs = cmakeargs
        self.supports_non_srcdir_builds = True
        self.skip_install_phase = skip_install_phase
        self.force_non_srcdir_builds = False
        self.supports_install_destdir = True

    def eval_args(self, args):
        args = Package.eval_args(self, args)
        args = args.replace('${libsuffix}', '')
        return args

    def get_srcdir(self, buildscript):
        return self.branch.srcdir

    def get_builddir(self, buildscript):
        builddir = self.get_srcdir(buildscript)
        if buildscript.config.buildroot and self.supports_non_srcdir_builds:
            d = buildscript.config.builddir_pattern % (
                self.branch.checkoutdir or self.branch.get_module_basename())
            builddir = os.path.join(buildscript.config.buildroot, d)
        if self.force_non_srcdir_builds and builddir == self.get_srcdir(buildscript):
            builddir = os.path.join(builddir, 'build')
        return builddir

    def get_cmakeargs(self):
        args = '%s %s' % (self.cmakeargs,
                          self.config.module_cmakeargs.get(
                              self.name, self.config.cmakeargs))
        return self.eval_args(args)

    def do_configure(self, buildscript):
        buildscript.set_action(_('Configuring'), self)
        srcdir = self.get_srcdir(buildscript)
        builddir = self.get_builddir(buildscript)
        if not os.path.exists(builddir):
            os.makedirs(builddir)
        prefix = os.path.expanduser(buildscript.config.prefix)
        if not inpath('cmake', os.environ['PATH'].split(os.pathsep)):
            raise CommandError(_('%s not found') % 'cmake')
        baseargs = '-DCMAKE_INSTALL_PREFIX=%s -DCMAKE_INSTALL_LIBDIR=lib' % prefix
        cmakeargs = self.get_cmakeargs()
        # CMake on Windows generates VS projects or NMake makefiles by default.
        # When using MSYS "MSYS Makefiles" is the best guess. "Unix Makefiles"
        # and "MinGW Makefiles" could also work (each is a bit different).
        if os.name == 'nt' and os.getenv("MSYSCON") and '-G' not in cmakeargs:
            baseargs += ' -G "MSYS Makefiles"'
        cmd = 'cmake %s %s %s' % (baseargs, cmakeargs, srcdir)
        if os.path.exists(os.path.join(builddir, 'CMakeCache.txt')):
            # remove that file, as it holds the result of a previous cmake
            # configure run, and would be reused unconditionnaly
            # (cf https://bugzilla.gnome.org/show_bug.cgi?id=621194)
            # FIXME: It's always wrong to remove CMakeCache.txt without also removing CMakeFiles directory
            os.unlink(os.path.join(builddir, 'CMakeCache.txt'))
        buildscript.execute(cmd, cwd = builddir, extra_env = self.extra_env)
    do_configure.depends = [PHASE_CHECKOUT]
    do_configure.error_phases = [PHASE_FORCE_CHECKOUT]

    def do_clean(self, buildscript):
        buildscript.set_action(_('Cleaning'), self)
        builddir = self.get_builddir(buildscript)
        self.make(buildscript, 'clean')
    do_clean.depends = [PHASE_CONFIGURE]
    do_clean.error_phases = [PHASE_FORCE_CHECKOUT, PHASE_CONFIGURE]

    def do_build(self, buildscript):
        buildscript.set_action(_('Building'), self)
        builddir = self.get_builddir(buildscript)
        self.make(buildscript)
    do_build.depends = [PHASE_CONFIGURE]
    do_build.error_phases = [PHASE_FORCE_CHECKOUT]

    def do_dist(self, buildscript):
        buildscript.set_action(_('Creating tarball for'), self)
        self.make(buildscript, 'package_source')
    do_dist.depends = [PHASE_CONFIGURE]
    do_dist.error_phases = [PHASE_FORCE_CHECKOUT, PHASE_CONFIGURE]

    def skip_install(self, buildscript, last_phase):
        return self.config.noinstall or self.skip_install_phase

    def do_install(self, buildscript):
        buildscript.set_action(_('Installing'), self)
        builddir = self.get_builddir(buildscript)
        destdir = self.prepare_installroot(buildscript)
        self.make(buildscript, 'install DESTDIR={}'.format(destdir))
        self.process_install(buildscript, self.get_revision())
    do_install.depends = [PHASE_BUILD]

    def xml_tag_and_attrs(self):
        return 'cmake', [('id', 'name', None),
                         ('skip-install', 'skip_install_phase', False)]


def parse_cmake(node, config, uri, repositories, default_repo):
    instance = CMakeModule.parse_from_xml(node, config, uri, repositories, default_repo)

    instance.dependencies += ['cmake', instance.get_makecmd(config)]

    instance.cmakeargs = collect_args(instance, node, 'cmakeargs')
    instance.makeargs = collect_args(instance, node, 'makeargs')

    if node.hasAttribute('skip-install'):
        skip_install = node.getAttribute('skip-install')
        if skip_install.lower() in ('true', 'yes'):
            instance.skip_install_phase = True
        else:
            instance.skip_install_phase = False
    if node.hasAttribute('supports-non-srcdir-builds'):
        instance.supports_non_srcdir_builds = \
                (node.getAttribute('supports-non-srcdir-builds') != 'no')
    if node.hasAttribute('force-non-srcdir-builds'):
        instance.force_non_srcdir_builds = \
                (node.getAttribute('force-non-srcdir-builds') != 'no')
    return instance

register_module_type('cmake', parse_cmake)

