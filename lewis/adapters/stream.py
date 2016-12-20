# -*- coding: utf-8 -*-
# *********************************************************************
# lewis - a library for creating hardware device simulators
# Copyright (C) 2016 European Spallation Source ERIC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
# *********************************************************************

from __future__ import print_function

import asynchat
import asyncore
import inspect
import re
import socket
from argparse import ArgumentParser

from six import b

from lewis.adapters import Adapter, ForwardMethod, ForwardProperty
from lewis.core.utils import format_doc_text


class StreamHandler(asynchat.async_chat):
    def __init__(self, sock, target):
        asynchat.async_chat.__init__(self, sock=sock)
        self.set_terminator(b(target.in_terminator))
        self.target = target
        self.buffer = []

    def collect_incoming_data(self, data):
        self.buffer.append(data)

    def found_terminator(self):
        request = b''.join(self.buffer)
        reply = None
        self.buffer = []

        try:
            try:
                cmd = next(cmd for cmd in self.target.commands if cmd.can_process(request))
                reply = cmd.process_request(request, self.target)
            except StopIteration:
                raise RuntimeError('None of the device\'s commands matched.')
        except Exception as error:
            reply = self.target.handle_error(request, error)

        if reply is not None:
            self.push(b(reply + self.target.out_terminator))


class StreamServer(asyncore.dispatcher):
    def __init__(self, host, port, target):
        asyncore.dispatcher.__init__(self)
        self.target = target
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind((host, port))
        self.listen(5)

    def handle_accept(self):
        pair = self.accept()
        if pair is not None:
            sock, addr = pair
            print("Client connect from %s" % repr(addr))
            StreamHandler(sock, self.target)


class BaseCommand(object):
    member = None
    argument_mappings = None
    return_mapping = None
    doc = None

    def can_process(self, request):
        raise NotImplementedError('Commands must implement can_process method.')

    def process_request(self, request, target):
        raise NotImplementedError('Commands must implement process_request method.')

    @property
    def patterns(self):
        raise NotImplementedError('Commands must implement patterns property.')

    def map_arguments(self, arguments):
        """
        Returns the mapped function arguments. If no mapping functions are defined, the arguments
        are returned as they were supplied.

        :param arguments: List of arguments for bound function as strings.
        :return: Mapped arguments.
        """
        if self.argument_mappings is None:
            return arguments

        return [f(a) for f, a in zip(self.argument_mappings, arguments)]

    def map_return_value(self, return_value):
        if callable(self.return_mapping):
            return self.return_mapping(return_value)

        if self.return_mapping is not None:
            return self.return_mapping

        return return_value


class Cmd(BaseCommand):
    """
    This is a small helper class that makes it easy to define commands that are parsed
    by StreamAdapter and forwarded to the correct methods on the Adapter.

    Method arguments are indicated by groups in the regular expression. The number of
    groups has to match the number of arguments of the method. The optional argument_mappings
    can be an iterable of callables with one parameter of the same length as the
    number of arguments of the method. The first parameter will be transformed using the
    first function, the second using the second function and so on. This can be useful
    to automatically transform strings provided by the adapter into a proper data type
    such as ``int`` or ``float`` before they are passed to the method.

    The return_mapping argument is similar, it should map the return value of the method
    to a string. The default map function only does that when the supplied value
    is not None. It can also be set to a numeric value or a string constant so that the
    command always returns the same value. If it is ``None``, the return value is not
    modified at all.

    Finally, documentation can be provided by passing the doc-argument. If it is omitted,
    the docstring of the bound method is used and if that is not present, left empty.

    :param target_method: Method to be called when regex matches.
    :param regex: Regex to match for method call.
    :param argument_mappings: Iterable with mapping functions from string to some type.
    :param return_mapping: Mapping function for return value of method.
    :param doc: Description of the command. If not supplied, the docstring is used.
    """

    def __init__(self, target_method, pattern, argument_mappings=None,
                 return_mapping=lambda x: None if x is None else str(x), doc=None):
        self.member = target_method
        self.pattern = re.compile(b(pattern))

        if argument_mappings is not None and (self.pattern.groups != len(argument_mappings)):
            raise RuntimeError(
                'Expected {} argument mapping(s), got {}'.format(
                    self.pattern.groups, len(argument_mappings)))

        self.argument_mappings = argument_mappings
        self.return_mapping = return_mapping
        self.doc = doc

    @property
    def patterns(self):
        return [self.pattern.pattern]

    def can_process(self, request):
        return self.pattern.match(request) is not None

    def process_request(self, request, target):
        match = self.pattern.match(request)

        if not match:
            raise RuntimeError('Request can not be processed.')

        args = self.map_arguments(match.groups())
        func = getattr(target, self.member)

        return self.map_return_value(func(*args))


class Var(BaseCommand):
    def __init__(self, target_member, read_pattern=None, write_pattern=None,
                 argument_mappings=None, return_mapping=lambda x: None if x is None else str(x),
                 doc=None):
        self.member = target_member

        self._patterns = {key: re.compile(pattern) for key, pattern in
                          zip(('read', 'write'), (read_pattern, write_pattern)) if
                          pattern is not None}

        if 'read' in self._patterns and self._patterns['read'].groups != 0:
            raise RuntimeError(
                'Command regex for reading member \'{}\' contains '
                'arguments.'.format(target_member))

        if 'write' in self._patterns and self._patterns['write'].groups != 1:
            raise RuntimeError(
                'Command regex for writing member \'{}\' is expected to contain one '
                'argument, but it contains {}.'.format(
                    target_member, self._patterns['write'].groups))

        self.argument_mappings = argument_mappings
        self.return_mapping = return_mapping
        self.doc = doc

    @property
    def patterns(self):
        return [pattern.pattern for pattern in self._patterns.values()]

    def can_process(self, request):
        return any(pattern.match(request) is not None for pattern in self._patterns.values())

    def process_request(self, request, target):
        if 'read' in self._patterns:
            match = self._patterns['read'].match(request)

            if match:
                return self.map_return_value(getattr(target, self.member))

        if 'write' in self._patterns:
            match = self._patterns['write'].match(request)

            if match:
                args = self.map_arguments(match.groups())
                return self.map_return_value(setattr(target, self.member, *args))

        raise RuntimeError('Could not process request.')


class StreamAdapter(Adapter):
    """
    This class is used to provide a TCP-stream based interface to a device.

    Many hardware devices use a protocol that is based on exchanging text with a client via
    a TCP stream. Sometimes RS232-based devices are also exposed this way via an adapter-box.
    This adapter makes it easy to mimic such a protocol, in a subclass only three members must
    be overridden:

     - in_terminator, out_terminator: These define how lines are terminated when transferred
       to and from the device respectively. They are stripped/added automatically.
       The default is ``\\r``.
     - commands: A list of :class:`~Cmd`-objects that define mappings between protocol
       and device/interface methods.

    Commands are expressed as regular expressions, a simple example may look like this:

    .. sourcecode:: Python

        class SimpleDeviceStreamInterface(StreamAdapter):
            commands = [
                Cmd('set_speed', r'^S=([0-9]+)$', argument_mappings=[int]),
                Cmd('get_speed', r'^S\\?$')
            ]

            def set_speed(self, new_speed):
                self._device.speed = new_speed

            def get_speed(self):
                return self._device.speed

    The interface has two commands, ``S?`` to return the speed and ``S=10`` to set the speed
    to an integer value.

    As in the :class:`lewis.adapters.epics.EpicsAdapter`, it does not matter whether the
    wrapped method is a part of the device or of the interface, this is handled automatically.

    In addition, the :meth:`handle_error`-method can be overridden. It is called when an exception
    is raised while handling commands.

    :param device: The exposed device.
    :param arguments: Command line arguments.
    """
    protocol = 'stream'

    in_terminator = '\r'
    out_terminator = '\r'

    commands = None

    def __init__(self, device, arguments=None):
        super(StreamAdapter, self).__init__(device, arguments)

        if arguments is not None:
            self._options = self._parseArguments(arguments)

        self._server = None

        self._create_properties(self.commands)

    @property
    def documentation(self):

        commands = ['{}:\n{}'.format(
            cmd.pattern.pattern,
            format_doc_text(cmd.doc or inspect.getdoc(getattr(self, cmd.method)) or ''))
                    for cmd in self.commands]

        options = format_doc_text(
            'Listening on: {}\nPort: {}\nRequest terminator: {}\nReply terminator: {}'.format(
                self._options.bind_address, self._options.port,
                repr(self.in_terminator), repr(self.out_terminator)))

        return '\n\n'.join(
            [inspect.getdoc(self) or '',
             'Parameters\n==========', options, 'Commands\n========'] + commands)

    def start_server(self):
        """
        Starts the TCP stream server, binding to the configured host and port.
        Host and port are configured via the command line arguments.

        .. note:: The server does not process requests unless
                  :meth:`handle` is called in regular intervals.

        """
        self._server = StreamServer(self._options.bind_address, self._options.port, self)

    def _parseArguments(self, arguments):
        parser = ArgumentParser(description='Adapter to expose a device via TCP Stream')
        parser.add_argument('-b', '--bind-address', default='0.0.0.0',
                            help='IP Address to bind and listen for connections on')
        parser.add_argument('-p', '--port', type=int, default=9999,
                            help='Port to listen for connections on')
        return parser.parse_args(arguments)

    def _create_properties(self, cmds):
        patterns = set()
        for cmd in cmds:
            member = cmd.member

            if member not in dir(self):
                if member not in dir(self._device):
                    raise AttributeError('Can not find member \''
                                         + member + '\' in device or interface.')

                if callable(getattr(self._device, member)):
                    setattr(self, member, ForwardMethod(self._device, member))
                else:
                    setattr(type(self), member, ForwardProperty('_device', member, instance=self))

            cmd_patterns = set(cmd.patterns)

            if not cmd_patterns.isdisjoint(patterns):
                raise RuntimeError(
                    'The regular expression \'{}\' is '
                    'associated with multiple members.'.format(cmd.pattern.pattern))

            patterns.update(cmd_patterns)

        if len(patterns) < len(cmds):
            raise RuntimeError('Warning')

    def handle_error(self, request, error):
        """
        Override this method to handle exceptions that are raised during command processing.
        The default implementation does nothing, so that any errors are silently ignored.

        :param request: The request that resulted in the error.
        :param error: The exception that was raised.
        """
        pass

    def handle(self, cycle_delay=0.1):
        """
        Spend approximately ``cycle_delay`` seconds to process requests to the server.

        :param cycle_delay: S
        """
        asyncore.loop(cycle_delay, count=1)
