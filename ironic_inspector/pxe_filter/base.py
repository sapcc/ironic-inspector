# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Base code for PXE boot filtering."""

import contextlib
import functools
import os
import re

from automaton import exceptions as automaton_errors
from automaton import machines
from eventlet import semaphore
from futurist import periodics
from oslo_concurrency import lockutils
from oslo_config import cfg
from oslo_log import log
import stevedore

from ironic_inspector.common.i18n import _
from ironic_inspector.common import ironic as ir_utils
from ironic_inspector import node_cache
from ironic_inspector.pxe_filter import interface
from ironic_inspector import utils

CONF = cfg.CONF
LOG = log.getLogger(__name__)

_STEVEDORE_DRIVER_NAMESPACE = 'ironic_inspector.pxe_filter'

_EMAC_REGEX = 'EMAC=([0-9a-f]{2}(:[0-9a-f]{2}){5}) IMAC=.*'


class InvalidFilterDriverState(RuntimeError):
    """The fsm of the filter driver raised an error."""


class States(object):
    """PXE filter driver states."""
    uninitialized = 'uninitialized'
    initialized = 'initialized'


class Events(object):
    """PXE filter driver transitions."""
    initialize = 'initialize'
    sync = 'sync'
    reset = 'reset'


# a reset is always possible
State_space = [
    {
        'name': States.uninitialized,
        'next_states': {
            Events.initialize: States.initialized,
            Events.reset: States.uninitialized,
        },
    },
    {
        'name': States.initialized,
        'next_states': {
            Events.sync: States.initialized,
            Events.reset: States.uninitialized,
        },
    },
]


def locked_driver_event(event):
    """Call driver method having processed the fsm event."""
    def outer(method):
        @functools.wraps(method)
        def inner(self, *args, **kwargs):
            with self.lock, self.fsm_reset_on_error() as fsm:
                fsm.process_event(event)
                return method(self, *args, **kwargs)
        return inner
    return outer


class BaseFilter(interface.FilterDriver):
    """The generic PXE boot filtering interface implementation.

    This driver doesn't do anything but provides a basic synchronization and
    initialization logic for some drivers to reuse. Subclasses have to provide
    a custom sync() method.
    """

    fsm = machines.FiniteMachine.build(State_space)
    fsm.default_start_state = States.uninitialized

    def __init__(self):
        super(BaseFilter, self).__init__()
        # Configuration check
        if (CONF.pxe_filter.deny_unknown_macs
                and CONF.processing.node_not_found_hook):
            msg = _('Configuration error: [pxe_filter]deny_unknown_macs is'
                    'enabled and [processing]node_not_found_hook is enabled.'
                    'These options cannot both be enabled simultaneously.')
            raise utils.Error(msg)

        self.lock = semaphore.BoundedSemaphore()
        self.fsm.initialize(start_state=States.uninitialized)

    def __str__(self):
        return '%(driver)s, state=%(state)s' % {
            'driver': type(self).__name__, 'state': self.state}

    @property
    def state(self):
        """Current driver state."""
        return self.fsm.current_state

    def reset(self):
        """Reset internal driver state.

        This method is called by the fsm_context manager upon exception as well
        as by the tear_down_filter method. A subclass might wish to override as
        necessary, though must not lock the driver. The overriding subclass
        should up-call.

        :returns: nothing.
        """
        LOG.debug('Resetting the PXE filter driver %s', self)
        # a reset event is always possible
        self.fsm.process_event(Events.reset)

    @contextlib.contextmanager
    def fsm_reset_on_error(self):
        """Reset the filter driver upon generic exception.

        The context is self.fsm. The automaton.exceptions.NotFound error is
        cast to the InvalidFilterDriverState error. Other exceptions trigger
        self.reset()

        :raises: InvalidFilterDriverState
        :returns: nothing.
        """
        try:
            yield self.fsm
        except automaton_errors.NotFound as e:
            raise InvalidFilterDriverState(_('The PXE filter driver %(driver)s'
                                             ': my fsm encountered an '
                                             'exception: %(error)s') % {
                                                 'driver': self, 'error': e})
        except Exception as e:
            LOG.exception('The PXE filter %(filter)s encountered an '
                          'exception: %(error)s; resetting the filter',
                          {'filter': self, 'error': e})
            self.reset()
            raise

    @locked_driver_event(Events.initialize)
    def init_filter(self):
        """Base driver initialization logic. Locked.

        :raises: InvalidFilterDriverState
        :returns: nothing.
        """
        LOG.debug('Initializing the PXE filter driver %s', self)

    def tear_down_filter(self):
        """Base driver tear down logic. Locked.

        :returns: nothing.
        """
        LOG.debug('Tearing down the PXE filter driver %s', self)
        with self.lock:
            self.reset()

    @locked_driver_event(Events.sync)
    def sync(self, ironic):
        """Base driver sync logic. Locked.

        :param ironic: obligatory ironic client instance
        :returns: nothing.
        """

    def get_periodic_sync_task(self):
        """Get periodic sync task for the filter.

        The periodic task returned is casting the InvalidFilterDriverState
        to the periodics.NeverAgain exception to quit looping.

        :raises: periodics.NeverAgain
        :returns: a periodic task to be run in the background.
        """
        _cached_client = None

        def periodic_sync_task():
            nonlocal _cached_client
            if _cached_client is None:
                _cached_client = ir_utils.get_client()
            try:
                self.sync(_cached_client)
            except InvalidFilterDriverState as e:
                LOG.warning('Filter driver %s disabling periodic sync '
                            'task because of an invalid state.', self)
                raise periodics.NeverAgain(e)

        return periodics.periodic(
            # NOTE(milan): the periodic decorator doesn't support 0 as
            # a spacing value of (a switched off) periodic
            spacing=CONF.pxe_filter.sync_period or float('inf'),
            enabled=bool(CONF.pxe_filter.sync_period))(periodic_sync_task)


class NoopFilter(BaseFilter):
    """A trivial PXE boot filter."""

    def get_periodic_sync_task(self):
        return None


_DRIVER_MANAGER = None


@lockutils.synchronized(__name__)
def _driver_manager():
    """Create a Stevedore driver manager for filtering drivers. Locked."""
    global _DRIVER_MANAGER

    name = CONF.pxe_filter.driver

    if _DRIVER_MANAGER is None:
        _DRIVER_MANAGER = stevedore.driver.DriverManager(
            _STEVEDORE_DRIVER_NAMESPACE,
            name=name,
            invoke_on_load=True
        )

    return _DRIVER_MANAGER


def driver():
    """Get the driver for the PXE filter.

    :returns: the singleton PXE filter driver object.
    """
    return _driver_manager().driver


def _ib_mac_to_rmac_mapping(ports):
    """Update port InfiniBand MAC address to EthernetOverInfiniBand MAC

    On InfiniBand deployment we need to map between the baremetal host
    InfiniBand MAC to the EoIB MAC. The EoIB MAC addresses are learned
    automatically by the EoIB interfaces and those MACs are recorded to the
    /sys/class/net/<ethoib_interface>/eth/neighs file. The InfiniBand GUID is
    taken from the ironic port client-id extra attribute. The InfiniBand GUID
    is the last 8 bytes of the client-id. The file format allows to map the
    GUID to EoIB MAC. The filter rules based on those MACs get applied during a
    driver.update() call

    :param ports: list of ironic ports
    :returns: Nothing.
    """
    ethoib_interfaces = CONF.iptables.ethoib_interfaces
    for iface in ethoib_interfaces:
        neighs_file = (os.path.join('/sys/class/net', iface, 'eth/neighs'))
        try:
            with open(neighs_file, 'r') as fd:
                data = fd.read()
        except IOError:
            LOG.error('Interface %s is not Ethernet Over InfiniBand; '
                      'Skipping ...', iface)
            continue
        for port in ports:
            client_id = port.extra.get('client-id')
            if client_id:
                # Note(moshele): The last 8 bytes in the client-id is
                # the baremetal node InfiniBand GUID
                guid = client_id[-23:]
                p = re.compile(_EMAC_REGEX + guid)
                match = p.search(data)
                if match:
                    port.address = match.group(1)


def get_ironic_macs(ironic):
    ports = [port for port in
             ir_utils.call_with_retries(ironic.ports, limit=None,
                                        fields=['address', 'extra'])]
    _ib_mac_to_rmac_mapping(ports)
    return {port.address for port in ports}


def get_inactive_macs(ironic):
    ports = [port for port in
             ir_utils.call_with_retries(ironic.ports, limit=None,
                                        fields=['address', 'extra'])
             if port.address not in node_cache.active_macs()]
    _ib_mac_to_rmac_mapping(ports)
    return {port.address for port in ports}


def get_active_macs(ironic):
    ports = [port for port in
             ir_utils.call_with_retries(ironic.ports, limit=None,
                                        fields=['address', 'extra'])
             if port.address in node_cache.active_macs()]
    _ib_mac_to_rmac_mapping(ports)
    return {port.address for port in ports}
