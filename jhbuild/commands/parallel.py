
from optparse import make_option
import logging

import jhbuild.moduleset
import jhbuild.frontends.parallel
from jhbuild.commands import Command, BuildCommand, register_command

class cmd_parallel_build(BuildCommand):
    doc = N_('Update and compile all modules in parallel (the default)')

    name = 'parallel'
    usage_args = N_('[ options ... ] [ modules ... ]')

    def __init__(self):
        Command.__init__(self, [
            make_option('-q', '--quiet',
                        action='store_true', dest='quiet', default=False,
                        help=_('quiet (no output)')),
            make_option('-t', '--start-at', metavar='MODULE',
                        action='store', dest='startat', default=None,
                        help=_('start building at the given module')),
            make_option('--build-optional-modules',
                        action='store_true', dest='build_optional_modules', default=False,
                        help=_('also build soft-dependencies that could be skipped')),
            make_option('-w', '--worker',
                        action='store', dest='num_worker', default=2,
                        help=_('number of workers')),
            make_option('-l', '--log',
                        action='store', dest='log_dir', default=None,
                        help=_('directory path where log files are stored')),

            ])

    def run(self, config, options, args, help=None):
        config.set_from_cmdline_options(options)
        module_set = jhbuild.moduleset.load(config)
        modules = args or config.modules
        full_module_list = module_set.get_full_module_list \
                               (modules, config.skip,
                                include_suggests=not config.ignore_suggests,
                                include_afters=options.build_optional_modules)
        full_module_list = module_set.remove_tag_modules(full_module_list,
                                                         config.tags)
        module_list = module_set.remove_system_modules(full_module_list)
        # remove modules up to startat
        if options.startat:
            while module_list and module_list[0].name != options.startat:
                del module_list[0]
            if not module_list:
                raise FatalError(_('%s not in module list') % options.startat)

        if len(module_list) == 0 and modules[0] in (config.skip or []):
            logging.info(
                    _('requested module is in the ignore list, nothing to do.'))
            return 0

        if config.check_sysdeps:
            module_state = module_set.get_module_state(full_module_list)
            if not self.required_system_dependencies_installed(module_state):
                self.print_system_dependencies(module_state)
                raise FatalError(_('Required system dependencies not installed.'
                                   ' Install using the command %(cmd)s or to '
                                   'ignore system dependencies use command-line'
                                   ' option %(opt)s' \
                                   % {'cmd' : "'jhbuild sysdeps --install'",
                                      'opt' : '--nodeps'}))
        return jhbuild.frontends.parallel.ParallelBuildScript(config, module_list, module_set).build()

register_command(cmd_parallel_build)
