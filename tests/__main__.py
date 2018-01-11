import os
import os.path
import sys
import unittest


TEST_ROOT = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(TEST_ROOT)


def convert_argv(argv):
    help  = False
    args = []
    modules = set()
    for arg in argv:
        # Unittest's main has only flags and positional args.
        # So we don't worry about options with values.
        if not arg.startswith('-'):
            # It must be the name of a test, case, module, or file.
            # We convert filenames to module names.  For filenames
            # we support specifying a test name by appending it to
            # the filename with a ":" in between.
            mod, _, test = arg.partition(':')
            if mod.endswith(os.sep):
                mod = mod.rsplit(os.sep, 1)[0]
            mod = mod.rsplit('.py', 1)[0]
            mod = mod.replace(os.sep, '.')
            arg = mod if not test else mod + '.' + test
            modules.add(mod)
        elif arg in ('-h', '--help'):
            help = True
        args.append(arg)

    cmd = [sys.executable + ' -m unittest']  # ...how unittest.main() likes it.
    if not modules and not help:
        # Do discovery.
        cmd += ['discover',
                '--start-directory', PROJECT_ROOT,
                ]
    return cmd + args


if __name__ == '__main__':
    argv = convert_argv(sys.argv[1:])
    unittest.main(module=None, argv=argv)
