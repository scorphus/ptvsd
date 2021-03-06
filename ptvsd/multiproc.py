# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See LICENSE in the project root
# for license information.

from __future__ import print_function, with_statement, absolute_import

import atexit
import itertools
import os
import re
import socket
import sys
import time
import traceback

try:
    import queue
except ImportError:
    import Queue as queue

from . import options
from .socket import create_server, create_client
from .messaging import JsonIOStream, JsonMessageChannel
from ._util import new_hidden_thread, debug

from _pydev_bundle import pydev_monkey
from _pydevd_bundle.pydevd_comm import get_global_debugger


subprocess_listener_socket = None

subprocess_queue = queue.Queue()
"""A queue of incoming 'ptvsd_subprocess' notifications. Whenenever a new request
is received, a tuple of (subprocess_request, subprocess_response) is placed in the
queue.

subprocess_request is the body of the 'ptvsd_subprocess' notification request that
was received, with additional information about the root process added.

subprocess_response is the body of the response that will be sent to respond to the
request. It contains a single item 'incomingConnection', which is initially set to
False. If, as a result of processing the entry, the subprocess shall receive an
incoming DAP connection on the port it specified in the request, its value should be
set to True, indicating that the subprocess should wait for that connection before
proceeding. If no incoming connection is expected, it is set to False, indicating
that the subprocess shall proceed executing user code immediately.

subprocess_queue.task_done() must be invoked for every subprocess_queue.get(), for
the corresponding subprocess_response to be delivered back to the subprocess.
"""

root_start_request = None
"""The 'launch' or 'attach' request that started debugging in this process, in its
entirety (i.e. dict representation of JSON request). This information is added to
'ptvsd_subprocess' notifications before they're placed in subprocess_queue.
"""


def listen_for_subprocesses():
    """Starts a listener for incoming 'ptvsd_subprocess' notifications that
    enqueues them in subprocess_queue.
    """

    global subprocess_listener_socket
    assert subprocess_listener_socket is None

    subprocess_listener_socket = create_server('localhost', 0)
    atexit.register(stop_listening_for_subprocesses)
    new_hidden_thread('SubprocessListener', _subprocess_listener).start()


def stop_listening_for_subprocesses():
    global subprocess_listener_socket
    if subprocess_listener_socket is None:
        return
    try:
        subprocess_listener_socket.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    subprocess_listener_socket = None


def subprocess_listener_port():
    if subprocess_listener_socket is None:
        return None
    _, port = subprocess_listener_socket.getsockname()
    return port


def _subprocess_listener():
    counter = itertools.count(1)
    while subprocess_listener_socket:
        try:
            (sock, _) = subprocess_listener_socket.accept()
        except Exception:
            break
        stream = JsonIOStream.from_socket(sock)
        _handle_subprocess(next(counter), stream)


def _handle_subprocess(n, stream):
    class Handlers(object):
        def ptvsd_subprocess_request(self, request):
            # When child process is spawned, the notification it sends only
            # contains information about itself and its immediate parent.
            # Add information about the root process before passing it on.
            arguments = dict(request.arguments)
            arguments.update({
                'rootProcessId': os.getpid(),
                'rootStartRequest': root_start_request,
            })

            debug('ptvsd_subprocess: %r' % arguments)
            response = {'incomingConnection': False}
            subprocess_queue.put((arguments, response))
            subprocess_queue.join()
            return response

    name = 'SubprocessListener-%d' % n
    channel = JsonMessageChannel(stream, Handlers(), name)
    channel.start()


def notify_root(port):
    assert options.subprocess_of

    debug('Subprocess %d notifying root process at port %d' % (os.getpid(), options.subprocess_notify))
    conn = create_client()
    conn.connect(('localhost', options.subprocess_notify))
    stream = JsonIOStream.from_socket(conn)
    channel = JsonMessageChannel(stream)
    channel.start()

    # Send the notification about ourselves to root, and wait for it to tell us
    # whether an incoming connection is anticipated. This will be true if root
    # had successfully propagated the notification to the IDE, and false if it
    # couldn't do so (e.g. because the IDE is not attached). There's also the
    # possibility that connection to root will just drop, e.g. if it crashes -
    # in that case, just exit immediately.

    request = channel.send_request('ptvsd_subprocess', {
        'parentProcessId': options.subprocess_of,
        'processId': os.getpid(),
        'port': port,
    })

    try:
        response = request.wait_for_response()
    except Exception:
        print('Failed to send subprocess notification; exiting', file=sys.__stderr__)
        traceback.print_exc()
        sys.exit(0)

    if not response['incomingConnection']:
        debugger = get_global_debugger()
        while debugger is None:
            time.sleep(0.1)
            debugger = get_global_debugger()
        debugger.ready_to_run = True


def patch_args(args):
    """
    Patches a command line invoking Python such that it has the same meaning, but
    the process runs under ptvsd. In general, this means that given something like:

        python -R -Q warn -m app

    the result should be:

        python -R -Q warn -m ptvsd --host localhost --port 0 ... -m app

    Note that the first -m above is interpreted by Python, and the second by ptvsd.
    """

    assert options.multiprocess

    args = list(args)

    # First, let's find the target of the invocation. This is one of:
    #
    #   filename.py
    #   -m module_name
    #   -c "code"
    #   -
    #
    # This needs to take into account other switches that have values:
    #
    #   -Q -W -X --check-hash-based-pycs
    #
    # because in something like "-X -c", -c is a value, not a switch.
    expect_value = False
    for i, arg in enumerate(args):
        # Skip Python binary.
        if i == 0:
            continue

        if arg == '-':
            # We do not support debugging while reading from stdin, so just let this
            # process run without debugging.
            return args

        if expect_value:
            # Consume the value and move on.
            expect_value = False
            continue

        if not arg.startswith('-') or arg in ('-c', '-m'):
            # This is the target.
            break

        if arg.startswith('--'):
            expect_value = (arg == '--check-hash-based-pycs')
            continue

        # All short switches other than -c and -m can be combined together, including
        # those with values. So, instead of -R -B -v -Q old, we might see -RBvQ old.
        # Furthermore, the value itself can be concatenated with the switch, so rather
        # than -Q old, we might have -Qold. When switches are combined, any switch that
        # has a value "eats" the rest of the argument; for example, -RBQv is treated as
        # -R -B -Qv, and not as -R -B -Q -v. So, we need to check whether one of 'Q',
        # 'W' or 'X' was present somewhere in the arg, and whether there was anything
        # following it in the arg. If it was there but nothing followed after it, then
        # the switch is expecting a value.
        split = re.split(r'[QWX]', arg, maxsplit=1)
        expect_value = (len(split) > 1 and split[-1] != '')

    else:
        # Didn't find the target, so we don't know how to patch this command line; let
        # it run without debugging.
        return args

    if not args[i].startswith('-'):
        # If it was a filename, it can be a Python file, a directory, or a zip archive
        # that is treated as if it were a directory. However, ptvsd only supports the
        # first scenario. Distinguishing between these can be tricky, and getting it
        # wrong means that process fails to launch, so be conservative.
        if not args[i].endswith('.py'):
            return args

    # Now we need to inject the ptvsd invocation right before the target. The target
    # itself can remain as is, because ptvsd is compatible with Python in that respect.
    args[i:i] = [
        '-m', 'ptvsd',
        '--host', 'localhost',
        '--port', '0',
        '--wait',
        '--multiprocess',
        '--subprocess-of', str(os.getpid()),
        '--subprocess-notify', str(options.subprocess_notify or subprocess_listener_port()),
    ]

    return args


def patch_and_quote_args(args):
    # On Windows, pydevd expects arguments to be quoted and escaped as necessary, such
    # that simply concatenating them via ' ' produces a valid command line. This wraps
    # patch_args and applies quoting (quote_args contains platform check), so that the
    # implementation of patch_args can be kept simple.
    return pydev_monkey.quote_args(patch_args(args))
