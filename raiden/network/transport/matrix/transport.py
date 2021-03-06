import json
import time
from collections import defaultdict
from typing import TYPE_CHECKING
from urllib.parse import urlparse
from uuid import uuid4

import gevent
import structlog
from eth_utils import is_binary_address, to_normalized_address
from gevent.event import Event
from gevent.lock import RLock, Semaphore
from gevent.pool import Pool
from gevent.queue import JoinableQueue
from matrix_client.errors import MatrixHttpLibError, MatrixRequestError

from raiden.constants import EMPTY_SIGNATURE, Environment
from raiden.exceptions import RaidenUnrecoverableError, TransportError
from raiden.messages.abstract import Message, RetrieableMessage, SignedRetrieableMessage
from raiden.messages.healthcheck import Ping, Pong
from raiden.messages.synchronization import Delivered, Processed
from raiden.network.transport.matrix.client import (
    GMatrixClient,
    MatrixMessage,
    MatrixSyncMessages,
    Room,
    User,
)
from raiden.network.transport.matrix.utils import (
    JOIN_RETRIES,
    USER_PRESENCE_REACHABLE_STATES,
    AddressReachability,
    DisplayNameCache,
    UserAddressManager,
    UserPresence,
    join_broadcast_room,
    login,
    make_client,
    make_message_batches,
    make_room_alias,
    my_place_or_yours,
    validate_and_parse_message,
    validate_userid_signature,
)
from raiden.network.transport.utils import timeout_exponential_backoff
from raiden.settings import MatrixTransportConfig
from raiden.storage.serialization import DictSerializer
from raiden.storage.serialization.serializer import MessageSerializer
from raiden.transfer import views
from raiden.transfer.identifiers import CANONICAL_IDENTIFIER_UNORDERED_QUEUE, QueueIdentifier
from raiden.transfer.state import NetworkState, QueueIdsToQueues
from raiden.transfer.state_change import ActionChangeNodeNetworkState
from raiden.utils.formatting import to_checksum_address
from raiden.utils.logging import redact_secret
from raiden.utils.runnable import Runnable
from raiden.utils.typing import (
    Address,
    AddressHex,
    Any,
    Callable,
    ChainID,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    NewType,
    Optional,
    Tuple,
)

if TYPE_CHECKING:
    from raiden.raiden_service import RaidenService

log = structlog.get_logger(__name__)

_RoomID = NewType("_RoomID", str)
# Combined with 10 retries (``..utils.JOIN_RETRIES``) this will give a total wait time of ~15s
ROOM_JOIN_RETRY_INTERVAL = 0.1
ROOM_JOIN_RETRY_INTERVAL_MULTIPLIER = 1.55
# A RetryQueue is considered idle after this many iterations without a message
RETRY_QUEUE_IDLE_AFTER = 10


class _RetryQueue(Runnable):
    """ A helper Runnable to send batched messages to receiver through transport """

    class _MessageData(NamedTuple):
        """ Small helper data structure for message queue """

        queue_identifier: QueueIdentifier
        message: Message
        text: str
        # generator that tells if the message should be sent now
        expiration_generator: Iterator[bool]

    def __init__(self, transport: "MatrixTransport", receiver: Address) -> None:
        self.transport = transport
        self.receiver = receiver
        self._message_queue: List[_RetryQueue._MessageData] = list()
        self._notify_event = gevent.event.Event()
        self._lock = gevent.lock.Semaphore()
        self._idle_since: int = 0  # Counter of idle iterations
        super().__init__()
        self.greenlet.name = f"RetryQueue recipient:{to_checksum_address(self.receiver)}"

    @property
    def log(self) -> Any:
        return self.transport.log

    @staticmethod
    def _expiration_generator(
        timeout_generator: Iterable[float], now: Callable[[], float] = time.time
    ) -> Iterator[bool]:
        """Stateful generator that yields True if more than timeout has passed since previous True,
        False otherwise.

        Helper method to tell when a message needs to be retried (more than timeout seconds
        passed since last time it was sent).
        timeout is iteratively fetched from timeout_generator
        First value is True to always send message at least once
        """
        for timeout in timeout_generator:
            _next = now() + timeout  # next value is now + next generated timeout
            yield True
            while now() < _next:  # yield False while next is still in the future
                yield False

    def enqueue(self, queue_identifier: QueueIdentifier, message: Message) -> None:
        """ Enqueue a message to be sent, and notify main loop """
        assert queue_identifier.recipient == self.receiver
        with self._lock:
            already_queued = any(
                queue_identifier == data.queue_identifier and message == data.message
                for data in self._message_queue
            )
            if already_queued:
                self.log.warning(
                    "Message already in queue - ignoring",
                    receiver=to_checksum_address(self.receiver),
                    queue=queue_identifier,
                    message=redact_secret(DictSerializer.serialize(message)),
                )
                return
            timeout_generator = timeout_exponential_backoff(
                self.transport._config.retries_before_backoff,
                self.transport._config.retry_interval,
                self.transport._config.retry_interval * 10,
            )
            expiration_generator = self._expiration_generator(timeout_generator)
            self._message_queue.append(
                _RetryQueue._MessageData(
                    queue_identifier=queue_identifier,
                    message=message,
                    text=MessageSerializer.serialize(message),
                    expiration_generator=expiration_generator,
                )
            )
        self.notify()

    def enqueue_unordered(self, message: Message) -> None:
        """ Helper to enqueue a message in the unordered queue. """
        self.enqueue(
            queue_identifier=QueueIdentifier(
                recipient=self.receiver, canonical_identifier=CANONICAL_IDENTIFIER_UNORDERED_QUEUE
            ),
            message=message,
        )

    def notify(self) -> None:
        """ Notify main loop to check if anything needs to be sent """
        with self._lock:
            self._notify_event.set()

    def _check_and_send(self) -> None:
        """Check and send all pending/queued messages that are not waiting on retry timeout

        After composing the to-be-sent message, also message queue from messages that are not
        present in the respective SendMessageEvent queue anymore
        """
        if not self.transport.greenlet:
            self.log.warning("Can't retry", reason="Transport not yet started")
            return
        if self.transport._stop_event.ready():
            self.log.warning("Can't retry", reason="Transport stopped")
            return

        assert self._lock.locked(), "RetryQueue lock must be held while messages are being sent"

        # On startup protocol messages must be sent only after the monitoring
        # services are updated. For more details refer to the method
        # `RaidenService._initialize_monitoring_services_queue`
        if self.transport._prioritize_broadcast_messages:
            self.transport._broadcast_queue.join()

        self.log.debug("Retrying message(s)", receiver=to_checksum_address(self.receiver))
        status = self.transport._address_mgr.get_address_reachability(self.receiver)
        if status is not AddressReachability.REACHABLE:
            # if partner is not reachable, return
            self.log.debug(
                "Partner not reachable. Skipping.",
                partner=to_checksum_address(self.receiver),
                status=status,
            )
            return

        def message_is_in_queue(message_data: _RetryQueue._MessageData) -> bool:
            if message_data.queue_identifier not in self.transport._queueids_to_queues:
                # The Raiden queue for this queue identifier has been removed
                return False
            return any(
                isinstance(message_data.message, RetrieableMessage)
                and send_event.message_identifier == message_data.message.message_identifier
                for send_event in self.transport._queueids_to_queues[message_data.queue_identifier]
            )

        message_texts: List[str] = list()
        for message_data in self._message_queue[:]:
            # Messages are sent on two conditions:
            # - Non-retryable (e.g. Delivered)
            #   - Those are immediately remove from the local queue since they are only sent once
            # - Retryable
            #   - Those are retried according to their retry generator as long as they haven't been
            #     removed from the Raiden queue
            remove = False
            if isinstance(message_data.message, (Delivered, Ping, Pong)):
                # e.g. Delivered, send only once and then clear
                # TODO: Is this correct? Will a missed Delivered be 'fixed' by the
                #       later `Processed` message?
                remove = True
                message_texts.append(message_data.text)
            elif not message_is_in_queue(message_data):
                remove = True
                self.log.debug(
                    "Stopping message send retry",
                    queue=message_data.queue_identifier,
                    message=message_data.message,
                    reason="Message was removed from queue or queue was removed",
                )
            else:
                # The message is still eligible for retry, consult the expiration generator if
                # it should be retried now
                if next(message_data.expiration_generator):
                    message_texts.append(message_data.text)

            if remove:
                self._message_queue.remove(message_data)

        if message_texts:
            self.log.debug(
                "Send", receiver=to_checksum_address(self.receiver), messages=message_texts
            )
            for message_batch in make_message_batches(message_texts):
                self.transport._send_raw(self.receiver, message_batch)

    def _run(self) -> None:  # type: ignore
        msg = f"_RetryQueue started before transport._raiden_service is set"
        assert self.transport._raiden_service is not None, msg
        self.greenlet.name = (
            f"RetryQueue "
            f"node:{to_checksum_address(self.transport._raiden_service.address)} "
            f"recipient:{to_checksum_address(self.receiver)}"
        )
        # run while transport parent is running
        while not self.transport._stop_event.ready():
            # once entered the critical section, block any other enqueue or notify attempt
            with self._lock:
                self._notify_event.clear()
                if self._message_queue:
                    self._idle_since = 0
                    self._check_and_send()
                else:
                    self._idle_since += 1

            if self.is_idle:
                # There have been no messages to process for a while. Exit.
                # A new instance will be created by `MatrixTransport._get_retrier()` if necessary
                self.log.debug("Exiting idle RetryQueue", queue=self)
                return
            # wait up to retry_interval (or to be notified) before checking again
            self._notify_event.wait(self.transport._config.retry_interval)

    @property
    def is_idle(self) -> bool:
        return self._idle_since >= RETRY_QUEUE_IDLE_AFTER

    def __str__(self) -> str:
        return self.greenlet.name

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} for {to_normalized_address(self.receiver)}>"


class MatrixTransport(Runnable):
    _room_prefix = "raiden"
    _room_sep = "_"
    log = log

    def __init__(self, config: MatrixTransportConfig, environment: Environment) -> None:
        super().__init__()
        self._uuid = uuid4()
        self._config = config
        self._raiden_service: Optional["RaidenService"] = None

        if config.server == "auto":
            available_servers = config.available_servers
        elif urlparse(config.server).scheme in {"http", "https"}:
            available_servers = [config.server]
        else:
            raise TransportError('Invalid matrix server specified (valid values: "auto" or a URL)')

        def _http_retry_delay() -> Iterable[float]:
            # below constants are defined in raiden.app.App.DEFAULT_CONFIG
            return timeout_exponential_backoff(
                self._config.retries_before_backoff,
                self._config.retry_interval / 5,
                self._config.retry_interval,
            )

        self._client: GMatrixClient = make_client(
            self._handle_sync_messages,
            available_servers,
            http_pool_maxsize=4,
            http_retry_timeout=40,
            http_retry_delay=_http_retry_delay,
            environment=environment,
        )
        self._server_url = self._client.api.base_url
        self._server_name = config.server_name or urlparse(self._server_url).netloc

        self.greenlets: List[gevent.Greenlet] = list()

        self._address_to_retrier: Dict[Address, _RetryQueue] = dict()
        self._displayname_cache = DisplayNameCache()

        self._broadcast_rooms: Dict[str, Room] = dict()
        self._broadcast_queue: JoinableQueue[Tuple[str, Message]] = JoinableQueue()

        self._started = False
        self._starting = False

        self._stop_event = Event()
        self._stop_event.set()

        self._broadcast_event = Event()
        self._prioritize_broadcast_messages = True

        self._invite_queue: List[Tuple[_RoomID, dict]] = []

        self._address_mgr: UserAddressManager = UserAddressManager(
            client=self._client,
            displayname_cache=self._displayname_cache,
            address_reachability_changed_callback=self._address_reachability_changed,
            user_presence_changed_callback=self._user_presence_changed,
            _log_context={"transport_uuid": str(self._uuid)},
        )

        self._address_to_room_ids: Dict[Address, List[_RoomID]] = defaultdict(list)

        self._client.add_invite_listener(self._handle_invite)

        self._health_lock = Semaphore()

        # Forbids concurrent room creation.
        self.room_creation_lock: Dict[Address, RLock] = defaultdict(RLock)

    def __repr__(self) -> str:
        if self._raiden_service is not None:
            node = f" node:{to_checksum_address(self._raiden_service.address)}"
        else:
            node = ""

        return f"<{self.__class__.__name__}{node} id:{self._uuid}>"

    def start(  # type: ignore
        self,
        raiden_service: "RaidenService",
        whitelist: List[Address],
        prev_auth_data: Optional[str],
    ) -> None:
        if not self._stop_event.ready():
            raise RuntimeError(f"{self!r} already started")
        self.log.debug("Matrix starting")
        self._stop_event.clear()
        self._starting = True
        self._raiden_service = raiden_service

        self._address_mgr.start()

        try:
            login(
                client=self._client,
                signer=self._raiden_service.signer,
                prev_auth_data=prev_auth_data,
            )
        except ValueError:
            # `ValueError` may be raised if `get_user` provides invalid data to
            # the `User` constructor. This is either a bug in the login, that
            # tries to get the user after a failed login, or a bug in the
            # Matrix SDK.
            raise RaidenUnrecoverableError("Matrix SDK failed to properly set the userid")
        except MatrixHttpLibError:
            raise RaidenUnrecoverableError("The Matrix homeserver seems to be unavailable.")

        self.log = log.bind(
            current_user=self._user_id,
            node=to_checksum_address(self._raiden_service.address),
            transport_uuid=str(self._uuid),
        )

        self._initialize_first_sync()
        self._initialize_room_inventory()
        self._initialize_broadcast_rooms()

        self._client.create_sync_filter(self._broadcast_rooms)

        def on_success(greenlet: gevent.Greenlet) -> None:
            if greenlet in self.greenlets:
                self.greenlets.remove(greenlet)

        self._client.start_listener_thread(timeout_ms=self._config.sync_timeout)
        assert isinstance(self._client.sync_worker, gevent.Greenlet)
        self._client.sync_worker.link_exception(self.on_error)
        self._client.sync_worker.link_value(on_success)

        assert isinstance(self._client.message_worker, gevent.Greenlet)
        self._client.message_worker.link_exception(self.on_error)
        self._client.message_worker.link_value(on_success)
        self.greenlets = [self._client.sync_worker, self._client.message_worker]

        self._client.set_presence_state(UserPresence.ONLINE.value)

        # (re)start any _RetryQueue which was initialized before start
        for retrier in self._address_to_retrier.values():
            if not retrier:
                self.log.debug("Starting retrier", retrier=retrier)
                retrier.start()

        super().start()  # start greenlet
        self._starting = False
        self._started = True

        pool = Pool(size=10)
        greenlets = set(pool.apply_async(self.whitelist, [address]) for address in whitelist)
        gevent.joinall(greenlets, raise_error=True)

        self.log.debug("Matrix started", config=self._config)

        # Handle any delayed invites in the future
        self._schedule_new_greenlet(self._process_queued_invites, in_seconds_from_now=1)

    def _process_queued_invites(self) -> None:
        if self._invite_queue:
            self.log.debug("Processing queued invites", queued_invites=len(self._invite_queue))
            for room_id, state in self._invite_queue:
                self._handle_invite(room_id, state)
            self._invite_queue.clear()

    def _run(self) -> None:  # type: ignore
        """ Runnable main method, perform wait on long-running subtasks """
        # dispatch auth data on first scheduling after start
        assert self._raiden_service is not None, "_raiden_service not set"
        self.greenlet.name = (
            f"MatrixTransport._run node:{to_checksum_address(self._raiden_service.address)}"
        )
        try:
            # waits on _stop_event.ready()
            self._broadcast_worker()
            # children crashes should throw an exception here
        except gevent.GreenletExit:  # killed without exception
            self._stop_event.set()
            gevent.killall(self.greenlets)  # kill children
            raise  # re-raise to keep killed status
        except Exception:
            self.stop()  # ensure cleanup and wait on subtasks
            raise

    def stop(self) -> None:
        """ Try to gracefully stop the greenlet synchronously

        Stop isn't expected to re-raise greenlet _run exception
        (use self.greenlet.get() for that),
        but it should raise any stop-time exception """
        if self._stop_event.ready():
            return
        self.log.debug("Matrix stopping")
        self._stop_event.set()
        self._broadcast_event.set()

        for retrier in self._address_to_retrier.values():
            if retrier:
                retrier.notify()

        # Wait for retriers to exit, then discard them
        gevent.wait({r.greenlet for r in self._address_to_retrier.values()})
        self._address_to_retrier = {}

        self._address_mgr.stop()
        self._client.stop()  # stop sync_thread, wait on client's greenlets

        # wait on own greenlets, no need to get on them, exceptions should be raised in _run()
        gevent.wait(self.greenlets)

        self._client.set_presence_state(UserPresence.OFFLINE.value)

        # Ensure keep-alive http connections are closed
        self._client.api.session.close()

        self.log.debug("Matrix stopped", config=self._config)
        try:
            del self.log
        except AttributeError:
            # During shutdown the log attribute may have already been collected
            pass
        # parent may want to call get() after stop(), to ensure _run errors are re-raised
        # we don't call it here to avoid deadlock when self crashes and calls stop() on finally

    def whitelist(self, address: Address) -> None:
        """Whitelist `address` to accept its messages."""
        msg = (
            "Whitelisting can only be done after the Matrix client has been "
            "logged in. This is necessary because whitelisting will create "
            "the Matrix room."
        )
        assert self._user_id, msg

        self.log.debug("Whitelist", address=to_checksum_address(address))
        self._address_mgr.add_address(address)

        # Start the room creation early on. This reduces latency for channel
        # partners, because by removing the latency of creating the room on the
        # first message.
        #
        # This does not reduce latency for target<->initiator communication,
        # since the target may be the node with lower address, and therefore
        # the node that has to create the room.
        self._maybe_create_room_for_address(address)

    def start_health_check(self, node_address: Address) -> None:
        """Start healthcheck (status monitoring) for a peer

        It also whitelists the address to answer invites and listen for messages
        """
        self.whitelist(node_address)
        with self._health_lock:
            node_address_hex = to_normalized_address(node_address)
            self.log.debug("Healthcheck", peer_address=to_checksum_address(node_address))

            candidates = self._client.search_user_directory(node_address_hex)
            self._displayname_cache.warm_users(candidates)

            user_ids = {
                user.user_id
                for user in candidates
                if validate_userid_signature(user) == node_address
            }
            # Ensure network state is updated in case we already know about the user presences
            # representing the target node
            self._address_mgr.track_address_presence(node_address, user_ids)

    def send_async(self, queue_identifier: QueueIdentifier, message: Message) -> None:
        """Queue the message for sending to recipient in the queue_identifier

        It may be called before transport is started, to initialize message queues
        The actual sending is started only when the transport is started
        """
        # even if transport is not started, can run to enqueue messages to send when it starts
        receiver_address = queue_identifier.recipient

        if not is_binary_address(receiver_address):
            raise ValueError("Invalid address {}".format(to_checksum_address(receiver_address)))

        # These are not protocol messages, but transport specific messages
        if isinstance(message, (Delivered, Ping, Pong)):
            raise ValueError(f"Do not use send_async for {message.__class__.__name__} messages")

        self.log.debug(
            "Send async",
            receiver_address=to_checksum_address(receiver_address),
            message=redact_secret(DictSerializer.serialize(message)),
            queue_identifier=queue_identifier,
        )

        self._send_with_retry(queue_identifier, message)

    def broadcast(self, room: str, message: Message) -> None:
        """Broadcast a message to a public room.

        These rooms aren't being listened on and therefore no reply could be heard, so these
        messages are sent in a send-and-forget async way.
        The actual room name is composed from the suffix given as parameter and chain name or id
        e.g.: raiden_ropsten_discovery
        Params:
            room: name suffix as passed in config['broadcast_rooms'] list
            message: Message instance to be serialized and sent
        """
        self._broadcast_queue.put((room, message))
        self._broadcast_event.set()

    def _broadcast_worker(self) -> None:
        def _broadcast(room_name: str, serialized_message: str) -> None:
            if not any(suffix in room_name for suffix in self._config.broadcast_rooms):
                raise RuntimeError(
                    f'Broadcast called on non-public room "{room_name}". '
                    f"Known public rooms: {self._config.broadcast_rooms}."
                )
            room_name = make_room_alias(self.chain_id, room_name)
            if room_name not in self._broadcast_rooms:
                room = join_broadcast_room(self._client, f"#{room_name}:{self._server_name}")
                self._broadcast_rooms[room_name] = room

            existing_room = self._broadcast_rooms.get(room_name)
            assert existing_room, f"Unknown broadcast room: {room_name!r}"

            self.log.debug(
                "Broadcast",
                room_name=room_name,
                room=existing_room,
                data=serialized_message.replace("\n", "\\n"),
            )
            existing_room.send_text(serialized_message)

        while not self._stop_event.ready():
            self._broadcast_event.clear()
            messages: Dict[str, List[Message]] = defaultdict(list)
            while self._broadcast_queue.qsize() > 0:
                room_name, message = self._broadcast_queue.get()
                messages[room_name].append(message)
            for room_name, messages_for_room in messages.items():
                serialized_messages = (
                    MessageSerializer.serialize(message) for message in messages_for_room
                )
                for message_batch in make_message_batches(serialized_messages):
                    _broadcast(room_name, message_batch)
                for _ in messages_for_room:
                    # Every message needs to be marked as done.
                    # Unfortunately there's no way to do that in one call :(
                    # https://github.com/gevent/gevent/issues/1436
                    self._broadcast_queue.task_done()

            # Stop prioritizing broadcast messages after initial queue has been emptied
            self._prioritize_broadcast_messages = False
            self._broadcast_event.wait(self._config.retry_interval)

    @property
    def _queueids_to_queues(self) -> QueueIdsToQueues:
        assert self._raiden_service is not None, "_raiden_service not set"

        chain_state = views.state_from_raiden(self._raiden_service)
        return views.get_all_messagequeues(chain_state)

    @property
    def _user_id(self) -> Optional[str]:
        return getattr(self, "_client", None) and getattr(self._client, "user_id", None)

    @property
    def chain_id(self) -> ChainID:
        assert self._raiden_service is not None, "_raiden_service not set"
        return self._raiden_service.rpc_client.chain_id

    def _initialize_first_sync(self) -> None:
        msg = "The first sync requires the Matrix client to be properly authenticated."
        assert self._user_id, msg

        msg = (
            "The sync thread must not be started before the `_inventory_rooms` "
            "is executed, the listener for the inventory rooms must be set up "
            "before any messages can be processed."
        )
        assert self._client.sync_thread is None, msg
        assert self._client.message_worker is None, msg

        # Call sync to fetch the inventory rooms and new invites. At this point
        # the messages themselves should not be processed because the room
        # callbacks are not installed yet (this is done below). The sync limit
        # prevents fetching the messages.
        prev_sync_limit = self._client.set_sync_limit(0)
        # Need to reset this here, otherwise we might run into problems after a restart
        self._client.last_sync = float("inf")
        self._client._sync()
        self._client.set_sync_limit(prev_sync_limit)
        # Process the result from the sync executed above
        response_queue = self._client.response_queue
        while response_queue:
            token_response = response_queue.get(block=False)
            self._client._handle_response(token_response[1], first_sync=True)

    def _initialize_room_inventory(self) -> None:
        msg = "The rooms can only be inventoried after the first sync."
        assert self._client.sync_token, msg

        msg = (
            "The sync thread must not be started before the `_inventory_rooms` "
            "is executed, the listener for the inventory rooms must be set up "
            "before any messages can be processed."
        )
        assert self._client.sync_worker is None, msg
        assert self._client.message_worker is None, msg

        self.log.debug("Inventory rooms", rooms=self._client.rooms)
        rooms_to_leave = list()

        for room in self._client.rooms.values():
            room_aliases = set(room.aliases)
            if room.canonical_alias:
                room_aliases.add(room.canonical_alias)

            for broadcast_alias in self._config.broadcast_rooms:
                if broadcast_alias in room_aliases:
                    self._broadcast_rooms[broadcast_alias] = room
                    break

            if not self._is_broadcast_room(room):
                partner_address = self._extract_partner_addresses(room.get_joined_members())
                # should contain only one element which is the partner's address
                if len(partner_address) == 1:
                    self._set_room_id_for_address(partner_address[0], room.room_id)
                elif len(partner_address) > 1:
                    # multiple addresses are part of the room this should not happen
                    # room is set to be leaved after loop ends
                    rooms_to_leave.append(room)

            self.log.debug(
                "Found room", room=room, aliases=room.aliases, members=room.get_joined_members()
            )

        self._leave_unexpected_rooms(
            rooms_to_leave, "At least two different addresses in this room"
        )

    def _extract_partner_addresses(self, members: List[User]) -> List[Address]:
        assert self._raiden_service is not None, "_raiden_service not set"
        joined_partner_addresses = set(validate_userid_signature(user) for user in members)

        return [
            address
            for address in joined_partner_addresses
            if address is not None and self._raiden_service.address != address
        ]

    def _leave_unexpected_rooms(
        self, rooms_to_leave: List[Room], reason: str = "No reason given"
    ) -> None:
        assert self._raiden_service is not None, "_raiden_service not set"

        for room in rooms_to_leave:
            self.log.warning(
                "Leaving Room",
                reason=reason,
                room_aliases=room.aliases,
                room_id=room.room_id,
                partners=[user.user_id for user in room.get_joined_members()],
            )
            try:
                room.leave()
            except MatrixRequestError as ex:
                # At a later stage this should be changed to proper error handling
                raise TransportError("could not leave room due to request error.") from ex

    def _initialize_broadcast_rooms(self) -> None:
        msg = "To join the broadcast rooms the Matrix client to be properly authenticated."
        assert self._user_id, msg

        for suffix in self._config.broadcast_rooms:
            room_name = make_room_alias(self.chain_id, suffix)
            broadcast_room_alias = f"#{room_name}:{self._server_name}"

            if room_name not in self._broadcast_rooms:
                self.log.debug("Joining broadcast room", broadcast_room_alias=broadcast_room_alias)
                self._broadcast_rooms[room_name] = join_broadcast_room(
                    client=self._client, broadcast_room_alias=broadcast_room_alias
                )

    def _handle_invite(self, room_id: _RoomID, state: dict) -> None:
        """Handle an invite request.

        Always join a room, even if the partner is not whitelisted. That was
        previously done to prevent a malicious node from inviting and spamming
        the user. However, there are cases where nodes trying to create rooms
        for a channel might race and an invite would be received by one node
        which did not yet whitelist the inviting node, as a result the invite
        would wrongfully be ignored. This change removes the whitelist check.
        To prevent spam, we make sure we ignore presence updates and messages
        from non-whitelisted nodes.
        """
        if self._stop_event.ready():
            return

        if self._starting:
            self.log.debug("Queueing invite", room_id=room_id)
            self._invite_queue.append((room_id, state))
            return

        invite_events = [
            event
            for event in state["events"]
            if event["type"] == "m.room.member"
            and event["content"].get("membership") == "invite"
            and event["state_key"] == self._user_id
        ]

        if not invite_events or not invite_events[0]:
            self.log.debug("Invite: no invite event found", room_id=room_id)
            return  # there should always be one and only one invite membership event for us

        self.log.debug("Got invite", room_id=room_id)

        sender = invite_events[0]["sender"]
        user = self._client.get_user(sender)
        self._displayname_cache.warm_users([user])
        peer_address = validate_userid_signature(user)

        if not peer_address:
            self.log.debug(
                "Got invited to a room by invalid signed user - ignoring",
                room_id=room_id,
                user=user,
            )
            return

        sender_join_events = [
            event
            for event in state["events"]
            if event["type"] == "m.room.member"
            and event["content"].get("membership") == "join"
            and event["state_key"] == sender
        ]

        if not sender_join_events or not sender_join_events[0]:
            self.log.debug("Invite: no sender join event", room_id=room_id)
            return  # there should always be one and only one join membership event for the sender

        join_rules_events = [
            event for event in state["events"] if event["type"] == "m.room.join_rules"
        ]

        # room privacy as seen from the event
        private_room: bool = False
        if join_rules_events:
            join_rules_event = join_rules_events[0]
            private_room = join_rules_event["content"].get("join_rule") == "invite"

        # we join room and _set_room_id_for_address despite room privacy and requirements,
        # _get_room_ids_for_address will take care of returning only matching rooms and
        # _leave_unused_rooms will clear it in the future, if and when needed
        room: Optional[Room] = None
        last_ex: Optional[Exception] = None
        retry_interval = 0.1
        for _ in range(JOIN_RETRIES):
            try:
                room = self._client.join_room(room_id)
            except MatrixRequestError as e:
                last_ex = e
                if self._stop_event.wait(retry_interval):
                    break
                retry_interval = retry_interval * 2
            else:
                break
        else:
            assert last_ex is not None
            raise last_ex  # re-raise if couldn't succeed in retries

        assert room is not None, f"joining room {room} failed"

        if self._is_broadcast_room(room):
            # This shouldn't happen with well behaving nodes but we need to defend against it
            # Since we already are a member of all broadcast rooms, the `join()` above is in
            # effect a no-op
            self.log.warning("Got invite to broadcast room, ignoring", inviting_user=user)
            return

        # room state may not populated yet, so we populate 'invite_only' from event
        room.invite_only = private_room

        self._set_room_id_for_address(address=peer_address, room_id=room_id)

        self.log.debug(
            "Joined from invite",
            room_id=room_id,
            aliases=room.aliases,
            inviting_address=to_checksum_address(peer_address),
        )

    def _handle_text(self, room: Room, message: MatrixMessage) -> List[Message]:
        """Handle a single Matrix message.

        The matrix message is expected to be a NDJSON, and each entry should be
        a valid JSON encoded Raiden message.

        Return::
            If any of the validations fail emtpy is returned, otherwise a list
            contained all parsed messages is returned.
        """

        is_valid_type = (
            message["type"] == "m.room.message" and message["content"]["msgtype"] == "m.text"
        )
        if not is_valid_type:
            return []

        # Ignore our own messages
        sender_id = message["sender"]
        if sender_id == self._user_id:
            return []

        user = self._client.get_user(sender_id)
        self._displayname_cache.warm_users([user])

        peer_address = validate_userid_signature(user)
        if not peer_address:
            self.log.debug(
                "Ignoring message from user with an invalid display name signature",
                peer_user=user.user_id,
                room=room,
            )
            return []

        if self._is_broadcast_room(room):
            # This must not happen. Nodes must not listen on broadcast rooms.
            raise RuntimeError(
                f"Received message in broadcast room {room.aliases[0]}. Sending user: {user}"
            )

        if not self._address_mgr.is_address_known(peer_address):
            self.log.debug(
                "Ignoring message from non-whitelisted peer",
                sender=user,
                sender_address=to_checksum_address(peer_address),
                room=room,
            )
            return []

        # rooms we created and invited user, or were invited specifically by them
        room_ids = self._get_room_ids_for_address(peer_address)

        if room.room_id not in room_ids:
            self.log.debug(
                "Ignoring invalid message",
                peer_user=user.user_id,
                peer_address=to_checksum_address(peer_address),
                room=room,
                expected_room_ids=room_ids,
                reason="unknown room for user",
            )
            return []

        return validate_and_parse_message(message["content"]["body"], peer_address)

    def _handle_sync_messages(self, sync_messages: MatrixSyncMessages) -> bool:
        """ Handle text messages sent to listening rooms """
        if self._stop_event.ready():
            return False

        assert self._raiden_service is not None, "_raiden_service not set"

        all_messages: List[Message] = list()
        for room, room_messages in sync_messages:
            # TODO: Don't fetch messages from the broadcast rooms. #5535
            if not self._is_broadcast_room(room):
                for text in room_messages:
                    all_messages.extend(self._handle_text(room, text))

        # Remove this #3254
        for message in all_messages:
            if isinstance(message, (Processed, SignedRetrieableMessage)) and message.sender:
                delivered_message = Delivered(
                    delivered_message_identifier=message.message_identifier,
                    signature=EMPTY_SIGNATURE,
                )
                self._raiden_service.sign(delivered_message)
                retrier = self._get_retrier(message.sender)
                retrier.enqueue_unordered(delivered_message)

        self.log.debug("Incoming messages", messages=all_messages)

        self._raiden_service.on_messages(all_messages)

        return len(all_messages) > 0

    def _get_retrier(self, receiver: Address) -> _RetryQueue:
        """ Construct and return a _RetryQueue for receiver """
        retrier = self._address_to_retrier.get(receiver)
        # The RetryQueue may have exited due to being idle
        if retrier is None or retrier.greenlet.ready():
            retrier = _RetryQueue(transport=self, receiver=receiver)
            self._address_to_retrier[receiver] = retrier
            # Always start the _RetryQueue, otherwise `stop` will block forever
            # waiting for the corresponding gevent.Greenlet to complete. This
            # has no negative side-effects if the transport has stopped because
            # the retrier itself checks the transport running state.
            retrier.start()
            # ``Runnable.start()`` may re-create the internal greenlet
            retrier.greenlet.link_exception(self.on_error)
        return retrier

    def _send_with_retry(self, queue_identifier: QueueIdentifier, message: Message) -> None:
        retrier = self._get_retrier(queue_identifier.recipient)
        retrier.enqueue(queue_identifier=queue_identifier, message=message)

    def _send_raw(self, receiver_address: Address, data: str) -> None:
        room = self._get_room_for_address(receiver_address, require_online_peer=True)

        if room:
            self.log.debug(
                "Send raw",
                receiver=to_checksum_address(receiver_address),
                room=room,
                data=data.replace("\n", "\\n"),
            )
            room.send_text(data)
        else:
            # It is possible there is no room yet. This happens when:
            #
            # - The room creation is started by a background thread running
            # `whitelist`, and the room can be used by a another thread.
            # - The room should be created by the partner, and this node is waiting
            # on it.
            # - No user for the requested address is online
            #
            # This is not a problem since the messages are retried regularly.
            self.log.warning(
                "No room for receiver", receiver=to_checksum_address(receiver_address)
            )

    def _get_room_for_address(
        self, address: Address, require_online_peer: bool = False
    ) -> Optional[Room]:
        msg = (
            f"address not health checked: "
            f"node: {self._user_id}, "
            f"peer: {to_checksum_address(address)}"
        )
        assert address and self._address_mgr.is_address_known(address), msg

        room_candidates = []
        room_ids = self._get_room_ids_for_address(address)
        if room_ids:
            while room_ids:
                room_id = room_ids.pop(0)
                room = self._client.rooms[room_id]
                if self._is_broadcast_room(room):
                    self.log.warning(
                        "Ignoring broadcast room for peer",
                        room=room,
                        peer=to_checksum_address(address),
                    )
                    continue
                room_candidates.append(room)

        if room_candidates:
            if not require_online_peer:
                # Return the first existing room
                room = room_candidates[0]
                self.log.debug(
                    "Existing room",
                    room=room,
                    members=room.get_joined_members(),
                    require_online_peer=require_online_peer,
                )
                return room
            else:
                # The caller needs a room with a peer that is online
                online_userids = {
                    user_id
                    for user_id in self._address_mgr.get_userids_for_address(address)
                    if self._address_mgr.get_userid_presence(user_id)
                    in USER_PRESENCE_REACHABLE_STATES
                }
                while room_candidates:
                    room = room_candidates.pop(0)
                    has_online_peers = online_userids.intersection(
                        {user.user_id for user in room.get_joined_members()}
                    )
                    if has_online_peers:
                        self.log.debug(
                            "Existing room",
                            room=room,
                            members=room.get_joined_members(),
                            require_online_peer=require_online_peer,
                        )
                        return room

        return None

    def _maybe_create_room_for_address(self, address: Address) -> None:
        if self._stop_event.ready():
            return None

        if self._get_room_for_address(address):
            return None

        assert self._raiden_service is not None, "_raiden_service not set"

        # The rooms creation is assymetric, only the node with the lower
        # address is responsible to create the room. This fixes race conditions
        # were the two nodes try to create a room with each other at the same
        # time, leading to communications problems if the nodes choose a
        # different room.
        #
        # This does not introduce a new attack vector, since not creating the
        # room is the same as being unresponsible.
        room_creator_address = my_place_or_yours(
            our_address=self._raiden_service.address, partner_address=address
        )
        if self._raiden_service.address != room_creator_address:
            self.log.debug(
                "This node should not create the room",
                partner_address=to_checksum_address(address),
            )
            return None

        with self.room_creation_lock[address]:
            candidates = self._client.search_user_directory(to_normalized_address(address))
            self._displayname_cache.warm_users(candidates)

            partner_users = [
                user for user in candidates if validate_userid_signature(user) == address
            ]
            partner_user_ids = [user.user_id for user in partner_users]

            if not partner_users:
                self.log.error(
                    "Partner doesn't have a user", partner_address=to_checksum_address(address)
                )

                return None

            room = self._client.create_room(None, invitees=partner_user_ids, is_public=False)
            self.log.debug("Created private room", room=room, invitees=partner_users)

            retry_interval = ROOM_JOIN_RETRY_INTERVAL
            for _ in range(JOIN_RETRIES):
                self.log.debug(
                    "Fetching room members",
                    room=room,
                    partner_address=to_checksum_address(address),
                )
                try:
                    members = room.get_joined_members(force_resync=True)
                except MatrixRequestError as e:
                    if e.code < 500:
                        raise

                # The display name signatures have been validated already.
                partner_joined = any(member.user_id in partner_user_ids for member in members)

                if partner_joined:
                    break

                if self._stop_event.wait(retry_interval):
                    return None

                retry_interval *= ROOM_JOIN_RETRY_INTERVAL_MULTIPLIER

                self.log.debug(
                    "Peer has not joined from invite yet, should join eventually",
                    room=room,
                    partner_address=to_checksum_address(address),
                    retry_interval=retry_interval,
                )

            # Here, the list of valid user ids is composed of
            # all known partner user ids along with our own.
            # If our partner roams, the user will be invited to
            # the room, resulting in multiple user ids for the partner.
            # If we roam, a new user and room will be created and only
            # the new user shall be in the room.
            valid_user_ids = partner_user_ids + [self._client.user_id]
            has_unexpected_user_ids = any(
                member.user_id not in valid_user_ids for member in members
            )

            if has_unexpected_user_ids:
                self._leave_unexpected_rooms([room], "Private room has unexpected participants")
                return None

            self._address_mgr.add_userids_for_address(
                address, {user.user_id for user in partner_users}
            )

            self._set_room_id_for_address(address, room.room_id)

            self.log.debug("Channel room", peer_address=to_checksum_address(address), room=room)
            return room

    def _is_broadcast_room(self, room: Room) -> bool:
        room_aliases = set(room.aliases)
        if room.canonical_alias:
            room_aliases.add(room.canonical_alias)
        return any(
            suffix in room_alias
            for suffix in self._config.broadcast_rooms
            for room_alias in room.aliases
        )

    def _user_presence_changed(self, user: User, _presence: UserPresence) -> None:
        # maybe inviting user used to also possibly invite user's from presence changes
        assert self._raiden_service is not None, "_raiden_service not set"
        greenlet = self._schedule_new_greenlet(self._maybe_invite_user, user)
        greenlet.name = (
            f"invite node:{to_checksum_address(self._raiden_service.address)} user:{user}"
        )

    def _address_reachability_changed(
        self, address: Address, reachability: AddressReachability
    ) -> None:
        if reachability is AddressReachability.REACHABLE:
            node_reachability = NetworkState.REACHABLE
            # _QueueRetry.notify when partner comes online
            retrier = self._address_to_retrier.get(address)
            if retrier:
                retrier.notify()
        elif reachability is AddressReachability.UNKNOWN:
            node_reachability = NetworkState.UNKNOWN
        elif reachability is AddressReachability.UNREACHABLE:
            node_reachability = NetworkState.UNREACHABLE
        else:
            raise TypeError(f'Unexpected reachability state "{reachability}".')

        assert self._raiden_service is not None, "_raiden_service not set"
        state_change = ActionChangeNodeNetworkState(address, node_reachability)
        self._raiden_service.handle_and_track_state_changes([state_change])

    def _maybe_invite_user(self, user: User) -> None:
        """ Invite user if necessary.

        - Only the node with the smallest address should do
          the invites, just like the rule to
          prevent race conditions while creating the room.

        - Invites are necessary for roaming, when the higher
          address node roams, a new user is created. Therefore, the new
          user will not be in the room because the room is private.
          This newly created user has to be invited.
        """
        msg = "Invite user must not be called on a non-started transport"
        assert self._raiden_service is not None, msg

        peer_address = validate_userid_signature(user)
        if not peer_address:
            return

        room_ids = self._get_room_ids_for_address(peer_address)
        if not room_ids:
            return

        if len(room_ids) >= 2:
            # TODO: Handle malicious partner creating
            # and additional room.
            # This cannot lead to loss of funds,
            # it is just unexpected behavior.
            self.log.debug(
                f"Multiple rooms exist with peer",
                peer_address=to_checksum_address(peer_address),
                rooms=room_ids,
            )

        inviter = my_place_or_yours(
            our_address=self._raiden_service.address, partner_address=peer_address
        )
        if inviter != self._raiden_service.address:
            self.log.debug(
                "This node is not the inviter", inviter=to_checksum_address(peer_address)
            )
            return

        room = self._client.rooms[room_ids[0]]
        if not room._members:
            room.get_joined_members(force_resync=True)
        if user.user_id not in room._members:
            self.log.debug(
                "Inviting", peer_address=to_checksum_address(peer_address), user=user, room=room
            )
            try:
                room.invite_user(user.user_id)
            except (json.JSONDecodeError, MatrixRequestError):
                self.log.warning(
                    "Exception inviting user, maybe their server is not healthy",
                    peer_address=to_checksum_address(peer_address),
                    user=user,
                    room=room,
                    exc_info=True,
                )

    def _sign(self, data: bytes) -> bytes:
        """ Use eth_sign compatible hasher to sign matrix data """
        assert self._raiden_service is not None, "_raiden_service not set"
        return self._raiden_service.signer.sign(data=data)

    def _set_room_id_for_address(self, address: Address, room_id: _RoomID) -> None:

        assert not room_id or room_id in self._client.rooms, "Invalid room_id"

        room_ids = self._get_room_ids_for_address(address)

        # push to front
        room_ids = [room_id] + [r for r in room_ids if r != room_id]
        self._address_to_room_ids[address] = room_ids

    def _get_room_ids_for_address(self, address: Address) -> List[_RoomID]:
        address_hex: AddressHex = to_checksum_address(address)
        room_ids = self._address_to_room_ids[address]

        self.log.debug("Room ids for address", for_address=address_hex, room_ids=room_ids)

        return [
            room_id
            for room_id in room_ids
            if room_id in self._client.rooms and self._client.rooms[room_id].invite_only
        ]
