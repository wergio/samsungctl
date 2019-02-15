# -*- coding: utf-8 -*-

import base64
import logging
import socket
import threading
import sys
from . import exceptions
from . import upnp
from .upnp.discover import auto_discover
from .utils import LogIt, LogItWithReturn

logger = logging.getLogger('samsungctl')


class RemoteLegacy(upnp.UPNPTV):
    """Object for remote control connection."""

    @LogIt
    def __init__(self, config):
        """Make a new connection."""
        self.sock = None
        self.config = config
        self.auth_lock = threading.Lock()
        self._loop_event = threading.Event()
        self._receive_lock = threading.Lock()
        self._receive_event = threading.Event()
        self._registered_callbacks = []
        super(RemoteLegacy, self).__init__(config)
        self._thread = None

        new_config, state = auto_discover.register_callback(
            self._connect,
            config.uuid
        )

        if not auto_discover.is_running:
            auto_discover.start()

        if state:
            self._connect(new_config, state)

    def _connect(self, config, power):
        if power and not self._thread:
            self.config.copy(config)

            if self.open():
                self.connect()
        elif not power:
            self.close()

    @property
    @LogItWithReturn
    def power(self):
        with self.auth_lock:
            return self.sock is not None
        # try:
        #     requests.get(
        #         'http://{0}:9090'.format(self.config.host),
        #         timeout=1
        #     )
        #     return True
        # except requests.ConnectTimeout:
        #     return False

    @power.setter
    @LogIt
    def power(self, value):
        if value and not self.power:
            logger.info('Power on is not supported for legacy TV\'s')
        elif not value and self.power:
            event = threading.Event()
            self.control('KEY_POWEROFF')

            while self.power:
                event.wait(2.0)

    def loop(self):
        while not self._loop_event.isSet():
            try:
                if self._read_response(self.sock):
                    continue
                else:
                    break

            except socket.error:
                self.disconnect()

        if self.sock is not None:
            self.sock.close()
            self.sock = None
            logging.debug("Connection closed.")

        self._thread = None
        self._loop_event.clear()

    @LogIt
    def open(self):
        if self.sock is not None:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((self.config.host, self.config.port))

            payload = (
                b"\x64\x00" +
                self._serialize_string(self.config.description) +
                self._serialize_string(self.config.id) +
                self._serialize_string(self.config.name)
            )
            packet = b"\x00\x00\x00" + self._serialize_string(payload, True)

            logger.info("Sending handshake.")
            sock.send(packet)
            response = self._read_response(sock, True)
            if response:
                self.sock = sock
                self._thread = threading.Thread(target=self.loop)
                self._thread.start()
            else:
                self.sock = None

            return True

        except socket.error:
            if not self.config.paired:
                raise RuntimeError('Unable to pair with TV.. Is the TV on?!?')
            else:
                self.sock = None
                return False

    @LogIt
    def close(self):
        """Close the connection."""
        if self._thread is not None:
            self._loop_event.set()
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
                self.sock.close()
            except socket.error:
                pass
            self._thread.join(2.0)

    @LogIt
    def control(self, key):
        """Send a control command."""
        if self.sock is None:
            return False

        with self._receive_lock:
            payload = b"\x00\x00\x00" + self._serialize_string(key)
            packet = b"\x00\x00\x00" + self._serialize_string(payload, True)

            logger.info("Sending control command: %s", key)

            def callback():
                self._receive_event.set()

            self._registered_callbacks += [callback]
            self._receive_event.clear()
            self.sock.send(packet)
            self._receive_event.wait(self._key_interval)
            self._registered_callbacks.remove(callback)

    _key_interval = 0.3

    @LogIt
    def _read_response(self, sock, first_time=False):
        try:
            header = sock.recv(3)
            logger.debug('header: ' + repr(header))
            tv_name_len = ord(header[1:2].decode('utf-8'))
            logger.debug('tv_name_len: ' + repr(tv_name_len))
            tv_name = sock.recv(tv_name_len)
            logger.debug('tv_name: ' + repr(tv_name))

            if first_time:
                logger.debug("Connected to '%s'.", tv_name.decode())

            response_len = sock.recv(2)
            logger.debug('response_len raw: ' + repr(response_len))

            response_len = ord(response_len[:1].decode('utf-8'))
            logger.debug('response_len: ' + repr(response_len))
            response = sock.recv(response_len)
            logger.debug('response: ' + repr(response))

            if len(response) == 0:
                return False

            if response == b"\x64\x00\x01\x00":
                logger.debug("Access granted.")
                self.config.paired = True
                return True
            elif response == b"\x64\x00\x00\x00":
                raise exceptions.AccessDenied()
            elif response[0:1] == b"\x0a":
                if first_time:
                    logger.warning("Waiting for authorization...")
                return self._read_response(sock)
            elif response[0:1] == b"\x65":
                logger.warning("Authorization cancelled.")
                raise exceptions.AccessDenied()
            elif response == b"\x00\x00\x00\x00":
                logger.debug("Control accepted.")
                if self._registered_callbacks:
                    self._registered_callbacks[0]()
                return True

            raise exceptions.UnhandledResponse(repr(response))

        except (
            exceptions.AccessDenied,
            exceptions.UnhandledResponse
        ):
            raise
        except:
            return False

    @staticmethod
    @LogItWithReturn
    def _serialize_string(string, raw=False):
        if isinstance(string, str):
            if sys.version_info[0] > 2:
                string = str.encode(string)

        if not raw:
            string = base64.b64encode(string)

        return bytes([len(string)]) + b"\x00" + string

    def __enter__(self):
        """
        Open the connection to the TV. use in a `with` statement

        >>> with samsungctl.Remote(config) as remote:
        >>>     remote.KEY_MENU()


        :return: self
        :rtype: :class: `samsungctl.Remote` instance
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        This gets called automatically when exiting a `with` statement
        see `samsungctl.Remote.__enter__` for more information

        :param exc_type: Not Used
        :param exc_val: Not Used
        :param exc_tb: Not Used
        :return: `None`
        """
        self.close()
