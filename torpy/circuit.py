# Copyright 2019 James Brown
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import socket
import logging
import threading
from contextlib import contextmanager
from selectors import DefaultSelector, EVENT_READ, EVENT_WRITE

from torpy.cells import CellRelayTruncated, CellRelayEnd, CellRelayData, CellRelaySendMe, CellRelayConnected, \
    CellDestroy, CircuitReason, CellCreate2, CellCreated2, RelayedTorCell, CellRelay, CellRelayExtend2, \
    CellRelayExtended2, CellRelayEarly, CellRelayEstablishRendezvous, CellRelayRendezvousEstablished, \
    CellRelayIntroduce1, CellRelayIntroduceAck
from torpy.utils import ignore
from torpy.stream import TorStream, TorWindow, StreamsManager
from torpy.crypto_state import CryptoState
from torpy.keyagreement import TapKeyAgreement, NtorKeyAgreement
from torpy.hiddenservice import DescriptorNotAvailable, HiddenServiceConnector

logger = logging.getLogger(__name__)


class CellTimeoutError(Exception):
    """Wait cell timeout error."""


class CircuitExtendError(Exception):
    """Circuit extend error."""


# tor ref: or.h
# #define ONION_HANDSHAKE_TYPE_TAP  0x0000
# #define ONION_HANDSHAKE_TYPE_FAST 0x0001
# #define ONION_HANDSHAKE_TYPE_NTOR 0x0002
class TorHandshakeType:
    TAP = 0
    FAST = 1
    NTOR = 2


class CircuitNode:
    def __init__(self, router, handshake_type=TorHandshakeType.NTOR):
        self._router = router
        self._handshake_type = handshake_type
        self._key_agreement = None
        self._crypto_state = None

        self._window = TorWindow()

    @property
    def router(self):
        return self._router

    @property
    def window(self):
        return self._window

    @property
    def handshake_type(self):
        return self._handshake_type

    @property
    def key_agreement(self):
        if not self._key_agreement:
            self._key_agreement = CircuitNode._get_key_agreement(self._router, self._handshake_type)
        return self._key_agreement

    @staticmethod
    def _get_key_agreement(onion_router, handshake_type):
        if handshake_type == TorHandshakeType.NTOR:
            return NtorKeyAgreement(onion_router)
        elif handshake_type == TorHandshakeType.TAP:
            return TapKeyAgreement(onion_router)
        else:
            raise NotImplementedError('Unknown key agreement')

    def create_onion_skin(self):
        return self.key_agreement.handshake

    def complete_handshake(self, handshake_data):
        shared_secret = self.key_agreement.complete_handshake(handshake_data)
        self._crypto_state = CryptoState(shared_secret)

    def encrypt_forward(self, relay_cell):
        self._crypto_state.encrypt_forward(relay_cell)

    def decrypt_backward(self, relay_cell):
        self._crypto_state.decrypt_backward(relay_cell)


def cells_format(cell_types):
    if isinstance(cell_types, list):
        return ' or '.join([c.__name__ for c in cell_types])
    else:
        return cell_types.__name__


class Waiter:
    def __init__(self, cell_types):
        self._cell_types = cell_types
        self._ev = threading.Event()
        self._read_cell = None
        self._err_msg = None

    def set_error(self, err_msg):
        self._err_msg = err_msg
        self._ev.set()

    def handler(self, cell):
        self._read_cell = cell
        self._ev.set()

    def is_set(self):
        return self._ev.is_set()

    def get(self, timeout=30):
        if not self._ev.wait(timeout):
            raise CellTimeoutError('Timeout wait for ' + cells_format(self._cell_types))
        if self._err_msg:
            raise Exception(self._err_msg)
        return self._read_cell


class TorReceiver(threading.Thread):
    def __init__(self, tor_socket, handler_mgr):
        super().__init__(name='RecvLoop_{}'.format(tor_socket.ip_address[0:7]))

        self._tor_socket = tor_socket

        self._handler_mgr = handler_mgr
        self._do_loop = False

        self._regs_funcs_map = {
            'reg': {
                socket.socket: self.register_socket,
                TorStream: self.register_stream
            },
            'unreg': {
                socket.socket: self.unregister_socket,
                TorStream: self.unregister_stream
            }
        }
        self._stream_to_callback = {}
        self._selector = DefaultSelector()

        self._cntrl_r, self._cntrl_w = socket.socketpair()
        self._selector.register(self._cntrl_r, EVENT_READ, self._do_stop)
        self._selector.register(self._tor_socket.ssl_socket, EVENT_READ, self._do_recv)

    def _cleanup(self):
        self._selector.unregister(self._cntrl_r)
        self._cntrl_w.close()
        self._cntrl_r.close()
        self._selector.unregister(self._tor_socket.ssl_socket)
        self._selector.close()

    def start(self):
        self._do_loop = True
        super().start()

    def stop(self):
        logger.debug('Stopping receiver thread...')
        self._cntrl_w.send(b'\1')
        self.join()

    def register(self, sock_or_stream, events, callback):
        func = self._regs_funcs_map['reg'].get(type(sock_or_stream))
        if not func:
            raise Exception('Unknown object for register')
        return func(sock_or_stream, events, callback)

    def register_socket(self, sock, events, callback):
        return self._selector.register(sock, events, callback)

    def register_stream(self, stream: TorStream, events, callback):
        if events & EVENT_WRITE:
            raise Exception('Write event not supported yet')
        stream.register(callback)
        if stream not in self._stream_to_callback:
            self._stream_to_callback[stream] = []
        self._stream_to_callback[stream].append(callback)

    def unregister(self, sock_or_stream):
        func = self._regs_funcs_map['unreg'].get(type(sock_or_stream))
        if not func:
            raise Exception('Unknown object for unregister')
        return func(sock_or_stream)

    def unregister_socket(self, sock):
        return self._selector.unregister(sock)

    def unregister_stream(self, stream):
        callbacks = self._stream_to_callback.pop(stream)
        for callback in callbacks:
            stream.unregister(callback)

    def _do_stop(self, raw_socket, mask):
        self._do_loop = False

    def _do_recv(self, raw_socket, mask):
        for cell in self._tor_socket.recv_cell_async():
            logger.debug('Cell received: %r', cell)
            try:
                self._handler_mgr.handle(cell)
            except BaseException:
                logger.exception("Some handle errors")

    def run(self):
        logger.debug("Starting...")
        while self._do_loop:
            events = self._selector.select()
            for key, mask in events:
                callback = key.data
                callback(key.fileobj, mask)

        self._cleanup()
        logger.debug("Stopped...")


class TorCircuitState:
    Unknown = 0
    Connected = 1
    Destroyed = 2


class CellHandlerManager:
    def __init__(self):
        self._handlers = {}

    # def stop(self):
    # # TODO: set_error for all waiters
    #    for handler in self._handlers:
    #

    def handle(self, cell, from_node=None, orig_cell=None):
        cell_type = type(cell)
        handlers = self._handlers.get(cell_type, [])

        if not handlers:
            logger.error('%s was received but no handlers for it', cell)
            return

        # Iterate over the copy to remove one-time handlers from the original array
        for handler in handlers[:]:
            if isinstance(handler, Waiter):
                # TODO: from_node=from_node
                handler.handler(cell)
                handlers.remove(handler)
            else:
                # TODO: always call with from_node?
                if from_node:
                    if orig_cell:
                        handler(cell, from_node, orig_cell)
                    else:
                        handler(cell, from_node)
                else:
                    handler(cell)

    @contextmanager
    def create_waiter(self, cell_types):
        logger.debug("Create waiter for %r", cells_format(cell_types))
        w = Waiter(cell_types)
        self.subscribe_for(cell_types, w)
        yield w
        # WARN: When cell_types is list we need remove other cell types handlers
        self.unsubscribe_for(cell_types, w)

    def unsubscribe_for(self, cell_types, handler):
        if isinstance(cell_types, list):
            for cell_type in cell_types:
                self._unsubscribe_for_cell(cell_type, handler)
        else:
            self._unsubscribe_for_cell(cell_types, handler)

    def _unsubscribe_for_cell(self, cell_type, handler):
        assert callable(handler) or isinstance(handler, Waiter)
        handlers = self._handlers.get(cell_type, [])
        if handler in handlers:
            handlers.remove(handler)

    def subscribe_for(self, cell_types, handler):
        if isinstance(cell_types, list):
            for cell_type in cell_types:
                self._subscribe_for_cell(cell_type, handler)
        else:
            self._subscribe_for_cell(cell_types, handler)

    def _subscribe_for_cell(self, cell_type, handler):
        assert callable(handler) or isinstance(handler, Waiter)
        if cell_type not in self._handlers:
            self._handlers[cell_type] = []
        self._handlers[cell_type].append(handler)


def check_connected(fn):
    def wrapped(self, *args, **kwargs):
        assert self.connected, 'Circuit must be connected first'
        return fn(self, *args, **kwargs)

    return wrapped


class TorCircuit:
    def __init__(self, id, router, sender, consensus, auth_data):
        self._id = id
        self._router = router
        self._sender = sender
        self._consensus = consensus

        self._handler_mgr = CellHandlerManager()
        self._stream_manager = StreamsManager(self, auth_data)

        self._relay_send_lock = threading.Lock()
        self._circuit_nodes = None
        self._state = TorCircuitState.Unknown
        self._state_lock = threading.Lock()
        self._guard = None
        self._associated_hs = None
        self._extend_lock = threading.Lock()

    def create(self, guard):
        with self._state_lock:
            assert self._state == TorCircuitState.Unknown, 'Circuit already connected'
            self._circuit_nodes = self._initialize(self._router)
            self._state = TorCircuitState.Connected
            self._guard = guard
            self._handler_mgr.subscribe_for(CellRelayTruncated, self._on_truncated)
            self._handler_mgr.subscribe_for(CellRelayEnd, self._on_stream_end)
            self._handler_mgr.subscribe_for([CellRelayData, CellRelaySendMe, CellRelayConnected], self._on_stream)
            logger.debug('Circuit created')

    def open_new_circuit(self, hops_count=0):
        return self._guard.open_circuit(hops_count)

    @contextmanager
    def create_new_circuit(self, hops_count=0):
        with self._guard.create_circuit(hops_count) as new_circuit:
            yield new_circuit

    def destroy(self, send_destroy=True):
        with self._state_lock:
            if self._state == TorCircuitState.Connected:
                # Destroy all streams belonging to the current circuit
                self.close_all_streams()
                if send_destroy:
                    # Destroy the circuit itself
                    self._send(CellDestroy(CircuitReason.FINISHED, self.id))
            elif self._state == TorCircuitState.Destroyed:
                logger.debug('#%x circuit has been destroyed already', self.id)
            else:
                raise Exception('#{:x} circuit is not yet connected'.format(self.id))

            self._state = TorCircuitState.Destroyed

    def close_all_streams(self):
        for stream in list(self._stream_manager.streams()):
            self.close_stream(stream)

    def _initialize(self, router):
        """
        Send CellCreate2 to create Circuit.

        Users set up circuits incrementally, one hop at a time. To create a
        new circuit, OPs send a CREATE/CREATE2 cell to the first node, with
        the first half of an authenticated handshake; that node responds with
        a CREATED/CREATED2 cell with the second half of the handshake.

        tor-spec.txt 5.1. "CREATE and CREATED cells"
        """
        logger.info('Creating new circuit #%x with %s router...', self.id, router)

        circuit_node = CircuitNode(router)
        onion_skin = circuit_node.create_onion_skin()

        cell_create = CellCreate2(circuit_node.handshake_type, onion_skin, self.id)
        cell_created2 = self._send_wait(cell_create, CellCreated2)

        logger.debug('Verifying response...')
        circuit_node.complete_handshake(cell_created2.handshake_data)

        return [circuit_node]

    @property
    def id(self):
        return self._id

    @property
    def nodes_count(self):
        return len(self._circuit_nodes)

    @property
    def last_node(self):
        return self._circuit_nodes[-1]

    @property
    def state(self):
        return self._state

    @property
    def connected(self):
        return self._state == TorCircuitState.Connected

    def handle_cell(self, cell):
        self._handler_mgr.handle(cell)

    def handle_relay(self, cell):
        # tor ref: circuit_receive_relay_cell
        # tor ref: connection_edge_process_relay_cell
        circuit_node, inner_cell = self._decrypt(cell)
        logger.debug('Decrypted relay cell received from %s: %r', circuit_node.router.nickname, inner_cell)
        self._handler_mgr.handle(inner_cell, from_node=circuit_node, orig_cell=cell)

    def _on_stream(self, cell, from_node, orig_cell):
        if self._sendme_process(cell, from_node, orig_cell):
            return

        stream = self._stream_manager.get_by_id(orig_cell.stream_id)
        if not stream:
            logger.warning('Stream #%i is already closed or was never opened', orig_cell.stream_id)
            return

        stream.handle_cell(cell)

    def _sendme_process(self, cell, from_node, orig_cell):
        cell_type = type(cell)
        if cell_type is CellRelaySendMe and not orig_cell.stream_id:
            from_node.window.package_inc()
            return True

        if cell_type is CellRelayData:
            from_node.window.deliver_dec()
            if from_node.window.need_sendme():
                self._send_relay(CellRelaySendMe(circuit_id=cell.circuit_id))
        return False

    def _on_stream_end(self, cell, from_node, orig_cell):
        stream = self._stream_manager.get_by_id(orig_cell.stream_id)
        if stream:
            self.close_stream(stream)

    def _on_truncated(self, cell, from_node, orig_cell):
        # tor ref: circuit_truncated
        logger.error('Circuit #%x was truncated by remote (%s)', self.id, cell.reason.name)
        self._guard.destroy_circuit(self)

    def _encrypt(self, relay_cell):
        # When a relay cell is sent from an OP, the OP encrypts the payload
        # with the stream cipher as follows:
        #    OP sends relay cell:
        #       For I=N...1, where N is the destination node:
        #          Encrypt with Kf_I.
        #       Transmit the encrypted cell to node 1.
        #
        # tor-spec.txt 5.5.2.1. "Routing from the Origin"
        assert isinstance(relay_cell, RelayedTorCell)
        assert not relay_cell.is_encrypted

        for circuit_node in self._circuit_nodes[::-1]:
            circuit_node.encrypt_forward(relay_cell)

    def _decrypt(self, relay_cell):
        # tor ref: relay_decrypt_cell
        assert relay_cell.is_encrypted

        from_node = None
        for i, circuit_node in enumerate(self._circuit_nodes):
            logger.debug("Decrypting by [%i] %s...", i, circuit_node.router)
            if not relay_cell.is_encrypted:
                logger.warning('Decrypted earlier')
                break

            # Continue decrypting...
            circuit_node.decrypt_backward(relay_cell)
            from_node = circuit_node

        return from_node, relay_cell.get_decrypted()

    def _send(self, cell):
        return self._sender.send(cell)

    @contextmanager
    def _create_waiter(self, wait_cell):
        # WARN: only for one thread things
        with self._handler_mgr.create_waiter(wait_cell) as w:
            yield w

    def _send_wait(self, cell, wait_cell):
        with self._handler_mgr.create_waiter(wait_cell) as w:
            self._send(cell)
            return w.get()

    def _send_relay(self, inner_cell, relay_type=None, stream_id=0):
        relay_type = relay_type or CellRelay
        assert issubclass(relay_type, RelayedTorCell)

        relay_cell = relay_type(inner_cell, stream_id=stream_id, circuit_id=self.id)
        with self._relay_send_lock:
            self._encrypt(relay_cell)
            self._send(relay_cell)

    def _send_relay_wait(self, inner_cell, wait_cells, relay_type=None, stream_id=0):
        with self._handler_mgr.create_waiter(wait_cells) as w:
            self._send_relay(inner_cell, relay_type=relay_type, stream_id=stream_id)
            logger.debug('Getting response...')
            return w.get()

    @check_connected
    def extend(self, next_onion_router, handshake_type=TorHandshakeType.NTOR):
        """
        Send CellExtend to extend this Circuit.

        To extend the circuit by a single onion router R_M, the OP performs these steps:
            1. Create an onion skin, encrypted to R_M's public onion key.
            2. Send the onion skin in a relay EXTEND2 cell along
               the circuit (see sections 5.1.2 and 5.5).
            3. When a relay EXTENDED/EXTENDED2 cell is received, verify KH,
               and calculate the shared keys.  The circuit is now extended.
        """
        logger.info('Extending the circuit #%x with %s...', self.id, next_onion_router)

        logger.debug('Sending Extend2...')
        extend_node = CircuitNode(next_onion_router, handshake_type)
        skin = extend_node.create_onion_skin()

        inner_cell = CellRelayExtend2(next_onion_router.ip, next_onion_router.tor_port,
                                      next_onion_router.fingerprint, skin)

        recv_cell = self._send_relay_wait(inner_cell,
                                          [CellRelayExtended2, CellRelayTruncated],
                                          relay_type=CellRelayEarly)

        if isinstance(recv_cell, CellRelayTruncated):
            raise CircuitExtendError("Extend error {}".format(recv_cell.reason.name))

        logger.debug('Verifying response...')
        extend_node.complete_handshake(recv_cell.handshake_data)

        self._circuit_nodes.append(extend_node)

    @check_connected
    def build_hops(self, hops_count):
        logger.info('Building %i hops circuit...', hops_count)
        while self.nodes_count < hops_count:
            if self.nodes_count == hops_count - 1:
                router = self._consensus.get_random_exit_node()
            else:
                router = self._consensus.get_random_middle_node()

            self.extend(router)
        logger.debug('Circuit has been built')

    @check_connected
    def open_stream(self, address=None):
        tor_stream = self._stream_manager.create_new()
        if address:
            tor_stream.connect(address)
        # WARN: you must call destroy_stream after usage
        return tor_stream

    @check_connected
    @contextmanager
    def create_stream(self, address=None):
        tor_stream = self.open_stream()
        try:
            if address:
                tor_stream.connect(address)
            yield tor_stream
        finally:
            self.close_stream(tor_stream)

    def close_stream(self, tor_stream):
        self._stream_manager.close(tor_stream)

    def _rendezvous_establish(self, rendezvous_cookie):
        assert len(rendezvous_cookie) == 20

        inner_cell = CellRelayEstablishRendezvous(rendezvous_cookie, self._id)
        cell_established = self._send_relay_wait(inner_cell, CellRelayRendezvousEstablished)
        # tor_ref: hs_client_receive_rendezvous_acked

        logger.info('Rendezvous established (%r)', cell_established)

    def _rendezvous_introduce(self, rendezvous_circuit, rendezvous_cookie, auth_type, descriptor_cookie):
        # tor ref: rend_client_send_introduction
        # tor ref: hs_circ_send_introduce1
        # tor ref: hs_client_send_introduce1
        # tor ref: connection_ap_handshake_attach_circuit

        introduction_point = self.last_node.router
        introducee = rendezvous_circuit.last_node.router

        # ! For Introduce we must use tap handshake
        extend_node = CircuitNode(introduction_point, handshake_type=TorHandshakeType.TAP)
        public_key_bytes = extend_node.key_agreement.public_key_bytes

        inner_cell = CellRelayIntroduce1(introduction_point, public_key_bytes,
                                         introducee, rendezvous_cookie, auth_type, descriptor_cookie, self._id)
        cell_ack = self._send_relay_wait(inner_cell, CellRelayIntroduceAck)
        logger.info('Introduced (%r)', cell_ack)

        return extend_node

    def extend_to_hidden(self, hidden_service):
        logger.info("Extending #%x circuit for hidden service %s...", self.id, hidden_service.hostname)

        with self._extend_lock:
            if self._associated_hs:
                if self._associated_hs.onion == hidden_service.onion:
                    logger.debug("Circuit #%x already associated with %s", self.id, hidden_service.onion)
                    return
                raise Exception("It's not possible associate one circuit to more then one hidden service")

            # TODO: Do we need to generate a new rendezvous_cookie every time?
            self._rendezvous_establish(hidden_service.rendezvous_cookie)

            # At any time, there are 6 hidden service directories responsible for
            # keeping replicas of a descriptor
            connector = HiddenServiceConnector(self, self._consensus)

            logger.info("Iterate over responsible dirs of the hidden service")
            for responsible_dir in connector.get_responsibles_dir(hidden_service):
                with ignore("Retry with next responsible dir", exceptions=(DescriptorNotAvailable,)):
                    logger.info("Iterate over introduction points of the hidden service")
                    for introduction in responsible_dir.get_introductions(hidden_service):
                        try:
                            # And finally try to agree to rendezvous with the hidden service
                            extend_node = introduction.connect(hidden_service)
                            self._circuit_nodes.append(extend_node)
                            self._associated_hs = hidden_service
                            return
                        except BaseException:
                            logger.exception('Some errors')
                            continue

            raise Exception("Can't extend to hidden service")


class CircuitsManager:
    LOCK = threading.Lock()
    GLOBAL_CIRCUIT_ID = 0

    def __init__(self, router, sender, consensus, auth_data):
        self._router = router
        self._sender = sender
        self._consensus = consensus
        self._auth_data = auth_data

        self._circuits_map = {}

    def circuits(self):
        for circuit in self._circuits_map.values():
            yield circuit

    @staticmethod
    def _get_next_circuit_id(msb=True):
        with CircuitsManager.LOCK:
            CircuitsManager.GLOBAL_CIRCUIT_ID += 1
            circuit_id = CircuitsManager.GLOBAL_CIRCUIT_ID
        if msb:
            circuit_id |= 0x80000000
        return circuit_id

    def create_new(self):
        circuit_id = self._get_next_circuit_id()
        circuit = TorCircuit(circuit_id, self._router, self._sender, self._consensus, self._auth_data)
        self._circuits_map[circuit.id] = circuit
        return circuit

    def get_by_id(self, circuit_id):
        return self._circuits_map.get(circuit_id, None)

    def remove(self, circuit_id):
        return self._circuits_map.pop(circuit_id, None)
