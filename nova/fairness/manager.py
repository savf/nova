#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Fairness Service."""

import libvirt
import os
import Queue
import sys

from oslo import messaging
from oslo.config import cfg

from nova import context
from nova import exception
from nova import manager
from nova import rpc
from nova import servicegroup
from nova.compute import api as compute_api
from nova.compute import rpcapi as compute_rpcapi
from nova.fairness import api as fairness_api
from nova.fairness import cloud_supply
from nova.fairness import metrics
from nova.fairness import resource_allocation
from nova.fairness import rui_stats
from nova.fairness import timing_stats
from nova.objects import instance as instance_objects
from nova.openstack.common import importutils
from nova.openstack.common import log as logging
from nova.openstack.common import periodic_task
from nova.openstack.common import timeutils
from nova.virt import driver
from nova.virt import virtapi

fairness_manager_opts = [
    cfg.StrOpt('active_metric',
               default='nova.fairness.metrics.greediness.GreedinessMetric',
               help='Fairness metric used to compute heavinesses.'),
    cfg.MultiStrOpt('available_metrics',
                    default=['nova.fairness.metrics.all_metrics'],
                    help='All available fairness metrics classes '
                    'that can be used.'),
    cfg.IntOpt('rui_collection_interval',
               default=10,
               help='Interval to collect RUI of all instances running '
                    'on the host. The interval is in seconds. Set to -1 '
                    'to disable. Setting this to 0 in Juno runs it at '
                    'the periodic task default rate.'),
    cfg.IntOpt('supply_poll_interval',
               default=10,
               help='Interval to check if all host supplies of all'
                    'compute hosts in the cloud have already been collected'
                    'and to poll hosts whose supplies are still missing.'),
    cfg.FloatOpt('resource_decay_factor',
                 default=0.5,
                 help='The decay factor is used to lessen the impact of old'
                      'RUI measurements on the newest measurement.'),
    cfg.BoolOpt('rui_stats_enabled',
                default=False,
                help='Set rui_stats_enabled to True in order to produce a'
                     'csv file containing resource reallocation as well'
                     'as resource utilization information about all'
                     'instances running on the host.'),
    cfg.BoolOpt('timing_stats_enabled',
                default=False,
                help='Set timing_stats_enabled to True in order to produce a'
                     'csv file containing timing information about the runtime'
                     'of the extension broken down into time for RUI collection,'
                     'heaviness calculation & distribution, CPU, Memory and Disk'
                     'reallocation as well as network bandwidth reallocation.')
    ]

CONF = cfg.CONF
fairness_group = cfg.OptGroup("fairness", "Fairness configuration options")
CONF.register_group(fairness_group)
CONF.register_opts(fairness_manager_opts, fairness_group)
LOG = logging.getLogger(__name__)


class FairnessManager(manager.Manager):
    """Manager to enforce cloud-wide, multi-resource fairness

    The manager collects RUI and host supply information
    """

    class RUICollectionHelper(object):

        def __init__(self, rui_statistics):
            self._last_collection_time = None
            self._time_since_last_collection = None
            self._full_demands = dict()
            self._interval_demands = dict()
            self._endowments = dict()
            self._rui_stats = rui_statistics

        def start(self):
            """ Indicate the point in time when the RUI collection started

            The time is needed to calculate the interval between RUI collections
            """
            time_now = timeutils.utcnow()
            if self._last_collection_time is not None:
                self._time_since_last_collection = timeutils.delta_seconds(
                    self._last_collection_time, time_now)
            self._last_collection_time = time_now

        def remove_inactive_instance(self, instance_name):
            """ Remove instances which are paused

            If the instance_name does not exist in _full_demands,
            _interval_demands or _endowments, pop does not produce
            a KeyError

            :param instance_name: The name of the instance
            :type instance_name: str
            """
            self._full_demands.pop(instance_name, 0)
            self._interval_demands.pop(instance_name, 0)
            self._endowments.pop(instance_name, 0)

        def last_collection_time(self):
            return self._last_collection_time

        def interval(self):
            return self._time_since_last_collection

        def add_instance_demand(self, demand):
            """ Add collected usage information for an instance

            On the initial run of the RUI collection method, utilization
            information for the instance since creation is gathered as
            the basis for future interval-based utilization information.
            For each interval, the difference between the current utilization
            information and the last recorded information is computed to create
            the interval-related demands

            :param demand: Instance usage information since creation
            :type demand: nova.fairness.metrics.BaseMetric.ResourceInformation
            """
            # If instance demands have already been collected, update the
            # collected demands by decaying them and adding the new demands
            if (demand.instance_name in self._interval_demands and
                    demand.instance_name in self._full_demands):
                last_full_demand = self._full_demands[demand.instance_name]
                new = metrics.BaseMetric.ResourceInformation(
                    cpu_time=demand.cpu_time - last_full_demand.cpu_time,
                    disk_bytes_read=demand.disk_bytes_read -
                    last_full_demand.disk_bytes_read,
                    disk_bytes_written=demand.disk_bytes_written -
                    last_full_demand.disk_bytes_written,
                    network_bytes_received=demand.network_bytes_received -
                    last_full_demand.network_bytes_received,
                    network_bytes_transmitted=demand.network_bytes_transmitted -
                    last_full_demand.network_bytes_transmitted,
                    memory_used=demand.memory_used,
                    compute_host=demand.compute_host,
                    user_id=demand.user_id,
                    instance_name=demand.instance_name
                )
                if self._interval_demands[demand.instance_name] is None:
                    self._interval_demands[demand.instance_name] = new
                else:
                    old = self._interval_demands[demand.instance_name]
                    assert isinstance(old,
                                      metrics.BaseMetric.ResourceInformation),\
                        "Old demand has to be a ResourceInformation object"
                    decay_factor = float(CONF.fairness.resource_decay_factor)
                    self._interval_demands[demand.instance_name] = \
                        (old * (1 - decay_factor)) + (new * decay_factor)
                self._full_demands[demand.instance_name] = demand
                if CONF.fairness.rui_stats_enabled:
                    self._rui_stats.add_rui(new,
                                            self._time_since_last_collection)
            else:
                # If the demands have not yet been stored earlier, this can
                # be because it's the first run of the RUI collection task
                # or the instance has been started after the service
                if self._time_since_last_collection is None:
                    self._interval_demands[demand.instance_name] = None
                else:
                    self._interval_demands[demand.instance_name] = demand
                self._full_demands[demand.instance_name] = demand

        def add_instance_endowment(self, endowment):
            """ Add endowment information for an instance

            :param endowment: Endowment for and instance
            :type endowment:
            nova.fairness.metrics.BaseMetric.ResourceInformation
            """
            self._endowments[endowment.instance_name] = endowment

        def get_instance_demands(self, instances):
            """ Return instance demands for all active instances

            Demand information stored in self._full_demands and
            self._interval_demands could still contain suspended/stopped or
            terminated instances so they are first removed based on the list
            of instances gathered by the RUI collection task

            :param instances: List of instances queried through nova conductor
            :type instances: nova.objects.instance.InstanceList
            :return: Demands of all active instances
            :rtype: dict
            """
            running_instances = set([instance.name for instance in instances])
            demand_instances = set(self._full_demands.keys())
            terminated_instances = demand_instances - running_instances
            for instance_name in terminated_instances:
                self._full_demands.pop(instance_name, 0)
                self._interval_demands.pop(instance_name, 0)
            # After the first RUI collection run, interval demands are still
            # None, so only full demands should be returned
            if not all(self._interval_demands.values()):
                return self._full_demands
            return self._interval_demands

        def get_instance_endowments(self, instances):
            """ Return instance endowments for all active instances

            Endowment information stored in self._endowments could still
            contain suspended/stopped or terminated instances so they are
            first removed based on the list of instances gathered by
            the RUI collection task

            :param instances: List of instances queried through nova conductor
            :type instances: nova.objects.instance.InstanceList
            :return: Endowmnets of all active instances
            :rtype: dict
            """
            running_instances = set([instance.name for instance in instances])
            endowment_instances = set(self._endowments.keys())
            terminated_instances = endowment_instances - running_instances
            for instance_name in terminated_instances:
                del self._endowments[instance_name]
            return self._endowments

    target = messaging.Target(version='1.0')

    def __init__(self, *args, **kwargs):
        self.fairness_api = fairness_api.API()
        self.compute_api = compute_api.API()
        self.compute_rpcapi = compute_rpcapi.ComputeAPI()
        self._active_metric = CONF.fairness.active_metric

        super(FairnessManager, self).__init__(service_name='fairness',
                                              *args, **kwargs)
        self.driver = driver.load_compute_driver(virtapi.VirtAPI,
                                                 'libvirt.LibvirtDriver')
        self.client = rpc.get_client(self.target, '1.0')
        self.servicegroup_api = servicegroup.API()
        self._fairness_quota =\
            metrics.BaseMetric.ResourceInformation(0, 0, 0, 0, 0, 0)
        self._fairness_heavinesses = dict()
        self._global_norm =\
            metrics.BaseMetric.ResourceInformation(0, 0, 0, 0, 0, 0)
        self._rui_stats = rui_stats.RUIStats()
        self._timing_stats = timing_stats.TimingStats()
        self._rui_collection_helper = self.RUICollectionHelper(self._rui_stats)
        self._cloud_supply = cloud_supply.CloudSupply()
        self._resource_allocation = \
            resource_allocation.ResourceAllocation(
                self._fairness_heavinesses,
                self._rui_stats,
                self._timing_stats,
                self._fairness_quota,
                self._global_norm)

    @staticmethod
    def _get_metric_class(metric_name):
        """ Check if a metric exists and return a full path to the metric

        This method walks through all available metrics in order to make sure
        that the metric really exists and the correct path can be constructed
        to later load the metric

        :param metric_name: The metric class-name
        :type metric_name: str
        :return: Full path to the metric or None
        :rtype: str
        """
        absolute_path = os.path.abspath(
                        sys.modules['nova.fairness.metrics'].__path__[0])
        for directory, directorynames, filenames in os.walk(absolute_path):
            for filename in filenames:
                root, ext = os.path.splitext(filename)
                if ext != ".py" or root == '__init__':
                    continue
                module = importutils.import_module(
                    'nova.fairness.metrics.' + root)
                for object_name in dir(module):
                    if object_name.startswith('_'):
                        continue
                    if object_name == metric_name:
                        return 'nova.fairness.metrics.' \
                                + root + '.' + object_name
        return None

    def set_metric(self, ctxt, metric_name):
        """ Set config entry for metric to be used

        This is the RPC-endpoint to set the metric through the nova API.
        The metric can however also be set through nova.conf with the entry
        'active_metric' in the group 'fairness'

        :param ctxt: Request context
        :type ctxt: nova.context.RequestContext object
        :param metric_name: Metric class-name
        :type metric_name: str
        :return: Success or failure of setting the metric
        :rtype: dict
        """
        result = dict()
        metric_module = self._get_metric_class(metric_name)
        if metric_module is not None:
            self._active_metric = metric_module
            result['status'] = "Metric successfully set."
        else:
            result['status'] = "Metric not found on compute host."
        return result

    @periodic_task.periodic_task(spacing=CONF.fairness.supply_poll_interval)
    def _complete_cloud_supply(self, ctxt):
        """ Complete the cloud supply by polling all missing hosts

        The local host supply is sent to each host in the list of missing hosts
        to provide them with the current host supply and to force them to send
        their own host supply back

        :param ctxt: The periodic task context
        :type ctxt: nova.context.RequestContext
        """
        self._cloud_supply.check_readiness()
        missing_hosts = self._cloud_supply.missing_hosts
        for host in missing_hosts:
            local_host_supply = self._cloud_supply.local_supply
            callcontext = self.client.prepare(topic='fairness',
                                              version='1.0',
                                              server=host)
            ctxt = context.RequestContext(None, None,
                                          remote_address=self.host)
            callcontext.cast(ctxt,
                             'receive_host_supply',
                             json_supply=local_host_supply.to_json())

    @periodic_task.periodic_task(spacing=CONF.fairness.rui_collection_interval)
    def _collect_rui(self, ctxt):
        """ Collect RUI of all instances running on the compute host

        :param ctxt: The periodic task context
        :type ctxt: nova.context.RequestContext
        """
        if self._cloud_supply.ready:
            self._timing_stats.start_timing("rui_setup")
            self._rui_collection_helper.start()
            instances = instance_objects.InstanceList().get_by_host(ctxt,
                                                                    self.host)
            if self._rui_collection_helper.interval() is None:
                host_uptime = timeutils.delta_seconds(
                    self._cloud_supply.local_boot_time,
                    self._rui_collection_helper.last_collection_time())
                _cloud_supply = self._cloud_supply.get_cloud_supply(host_uptime)
                _local_supply = self._cloud_supply.get_host_supply(host_uptime)
            else:
                _cloud_supply = self._cloud_supply.get_cloud_supply(
                    self._rui_collection_helper.interval())
                _local_supply = self._cloud_supply.get_host_supply(
                    self._rui_collection_helper.interval())
            user_count = self._resource_allocation.user_count
            if user_count is None:
                user_count = self._cloud_supply.user_count
            if user_count <= 0:
                user_count = 1
            self._fairness_quota.__dict__.update(
                    (_cloud_supply / user_count).__dict__)

            active_instances = 0
            total_vcpus = 0
            for instance in instances:
                total_vcpus += instance.vcpus
                domain = self.driver._lookup_by_name(instance['name'])
                if domain.isActive():
                    active_instances += 1
            self._timing_stats.stop_timing("rui_setup")
            if active_instances > 0:
                for instance in instances:
                    self._timing_stats.start_timing("rui", instance['name'])
                    domain = self.driver._lookup_by_name(instance['name'])
                    if not domain.isActive():
                        self._rui_collection_helper.remove_inactive_instance(
                            instance['name'])
                    else:
                        # Get CPU times
                        total_cpu_time = 0
                        try:
                            cputime = domain.vcpus()[0]
                            for i in range(len(cputime)):
                                total_cpu_time += cputime[i][2]
                        except libvirt.libvirtError:
                            pass
                        total_cpu_time /= 1000000000
                        total_cpu_time *= self._cloud_supply.local_bogo_mips
                        # Get disks transferred bytes
                        total_disks_bytes_read = 0
                        total_disks_bytes_written = 0
                        xml = domain.XMLDesc(0)
                        dom_io = self.driver._get_io_devices(xml)
                        for guest_disk in dom_io["volumes"]:
                            try:
                                stats = domain.blockStats(guest_disk)
                                total_disks_bytes_read += stats[1]
                                total_disks_bytes_written += stats[3]
                            except libvirt.libvirtError:
                                pass
                        # Get network transferred bytes
                        total_network_rx_bytes = 0
                        total_network_tx_bytes = 0
                        for interface in dom_io["ifaces"]:
                            try:
                                stats = domain.interfaceStats(interface)
                                total_network_rx_bytes += stats[0]
                                total_network_tx_bytes += stats[4]
                            except libvirt.libvirtError:
                                pass
                        # The memory reported by libvirt is
                        # measured in kilobytes
                        flavor_memory_total = domain.maxMemory()
                        total_memory_used = flavor_memory_total
                        try:
                            mem = domain.memoryStats()
                            if 'unused' in mem.keys():
                                total_memory_used = (flavor_memory_total -
                                                     int(mem['unused']))
                            elif 'rss' in mem.keys():
                                total_memory_used = int(mem['rss'])
                            if total_memory_used > flavor_memory_total:
                                total_memory_used = flavor_memory_total
                        except (libvirt.libvirtError, AttributeError):
                            pass

                        # Prepare instance demands
                        demand_resource = metrics.BaseMetric.\
                            ResourceInformation(
                                compute_host=instance['host'],
                                user_id=instance['user_id'],
                                instance_name=instance['name'],
                                cpu_time=total_cpu_time,
                                disk_bytes_read=total_disks_bytes_read,
                                disk_bytes_written=total_disks_bytes_written,
                                network_bytes_received=total_network_rx_bytes,
                                network_bytes_transmitted=
                                total_network_tx_bytes,
                                memory_used=total_memory_used)

                        self._rui_collection_helper.add_instance_demand(
                                demand_resource)

                        # Prepare instance endowments
                        flavor_cpu_time = (
                            (_local_supply.cpu_time / total_vcpus) *
                            instance['vcpus'])
                        endowment_resource = _local_supply / active_instances
                        endowment_resource.user_id = instance['user_id']
                        endowment_resource.instance_name = instance['name']
                        endowment_resource.cpu_time = flavor_cpu_time
                        endowment_resource.memory_used = flavor_memory_total
                        self._timing_stats.stop_timing("rui", instance['name'])
                        self._rui_collection_helper.add_instance_endowment(
                            endowment_resource)

            _instance_endowments =\
                self._rui_collection_helper.get_instance_endowments(instances)
            _instance_demands =\
                self._rui_collection_helper.get_instance_demands(instances)
            if len(_instance_endowments) > 0 and len(_instance_demands) > 0 \
                    and self._rui_collection_helper.interval() is not None:
                # Add the overcommitment to the cloud supply to consider
                # it for the global norm
                _cloud_supply *= self._cloud_supply.get_overcommitment()
                self._timing_stats.start_timing("heaviness")
                self._map_rui(
                        _cloud_supply,
                        _instance_endowments,
                        _instance_demands,
                        user_count)

    def _map_rui(self, supply, instance_endowments,
                 instance_demands, user_count):
        """ The RUI that has been collected is mapped to a heaviness-scalar

        The metric can be set in the nova.conf configuration file with the
        entry 'active_metric' in the group 'fairness'

        :param supply: Supply of all hosts in the cloud
        :type supply: nova.fairness.metrics.BaseMetric.ResourceInformation
        :param instance_endowments: Endowment information per instance
        :type instance_endowments: dict
        :param instance_demands: Actual resource consumption of instances
        :type instance_demands: dict
        :param user_count: Number of users running instances in the cloud
        :type user_count: int
        """
        metric_class = importutils.import_class(self._active_metric)
        result = metric_class().map(supply,
                                    instance_demands,
                                    instance_endowments,
                                    user_count)
        assert isinstance(result['global_norm'], list),\
            "The metric should return the global_norm as a list"
        norm = result['global_norm']
        self._global_norm.__init__(norm[0],
                                   norm[1], norm[2],
                                   norm[3], norm[4], norm[5])
        del result['global_norm']
        fairness_hosts = self.servicegroup_api.get_all("fairness")
        if not isinstance(fairness_hosts, exception.ServiceGroupUnavailable):
            ctxt = context.RequestContext(None, None, remote_address=self.host)
            for host in fairness_hosts:
                if host != self.host:
                    callcontext = self.client.prepare(topic='fairness',
                                                      version='1.0',
                                                      server=host)
                    callcontext.cast(ctxt,
                                     'receive_heavinesses',
                                     heavinesses=dict.copy(result))
            # Save own heavinesses without sending them through an RPC
            # to conserve bandwidth
            self.receive_heavinesses(ctxt, result)

    def _add_heavinesses(self, heavinesses):
        """ Add heavinesses received from a compute host

        The heavinesses for all instances of a host are stored into a queue
        which is itself stored in the self._fairness_heavinesses dictionary.
        This way, if one compute host happens to send a new collection of
        heavinesses while the other compute hosts are still computing the
        current heaviness, the new values can be stored in the queue
        for later use

        :param heavinesses: Instance heavinesses
        :type heavinesses: dict
        """
        if isinstance(heavinesses, dict):
            compute_host = heavinesses['compute_host']
            del heavinesses['compute_host']
            if (compute_host in self._fairness_heavinesses.keys() and
                isinstance(self._fairness_heavinesses[compute_host],
                           Queue.Queue)):
                self._fairness_heavinesses[compute_host].put(heavinesses)
                for key, value in heavinesses.iteritems():
                    if isinstance(value, dict):
                        LOG.debug("Instance "+key+" got heaviness: " +
                                  str(value['heaviness']))
            else:
                self._fairness_heavinesses[compute_host] = Queue.Queue()
                self._fairness_heavinesses[compute_host].put(heavinesses)

    def _all_heavinesses_collected(self):
        """ Check if all hosts have already reported their heavinesses

        The heavinesses are stored in Queues that belong to the specific host,
        so the queues have to be checked whether the hosts they hold heavinesses
        for are still online. If all queues then hold at least 1 heavinesses
        dictionary, heavinesses of all hosts have been collected

        :return: True if all heavinesses have been collected, False othwerwise
        :rtype: bool
        """
        fairness_hosts = self.servicegroup_api.get_all("fairness")
        if not isinstance(fairness_hosts, exception.ServiceGroupUnavailable):
            _local_heavinesses = dict.copy(self._fairness_heavinesses)
            for host in _local_heavinesses:
                if host not in fairness_hosts:
                    del self._fairness_heavinesses[host]
            # If all heavinesses queues contain at least one item, the priority
            # computation can begin.
            if all(not value.empty()
                   for key, value in self._fairness_heavinesses.iteritems()):
                return True
        return False

    def receive_heavinesses(self, ctxt, heavinesses):
        """ Receive heavinesses from host's RPC's

        The heavinesses are added into Queues per host and then all queues are
        checked in order to start the computation of priorities for resource
        reallocation

        :param ctxt: Request context
        :type ctxt: nova.context.RequestContext
        :param heavinesses: Instance heavinesses
        :type heavinesses: dict
        """
        formatted_time = ctxt.timestamp.strftime("%d.%m.%Y %H:%M:%S")
        LOG.debug("Received set of heavinesses created at " + formatted_time +
                  " on host " + ctxt.remote_address)
        self._add_heavinesses(heavinesses)
        if self._all_heavinesses_collected():
            self._timing_stats.stop_timing("heaviness")
            self._resource_allocation.reallocate()

    def _send_host_supply(self, host):
        """ Send the local host supply to a specific host

        The supply is only sent if the host is online and it's not localhost

        :param host: Host to send the supply to
        :type host: str
        """
        fairness_hosts = self.servicegroup_api.get_all("fairness")
        if not isinstance(fairness_hosts, exception.ServiceGroupUnavailable):
            if (host is not None and
                    host != self.host and
                    host in fairness_hosts):
                local_host_supply = self._cloud_supply.local_supply
                callcontext = self.client.prepare(topic='fairness',
                                                  version='1.0',
                                                  server=host)
                ctxt = context.RequestContext(None, None)
                callcontext.cast(ctxt,
                                 'receive_host_supply',
                                 json_supply=local_host_supply.to_json())

    def receive_host_supply(self, ctxt, json_supply):
        """ Receive host's supply information to build a cloud-wide supply

        The supply is deserialized from JSON and stored in the local CloudSupply
        object. To make sure that all hosts receive all host supply information,
        the origin of an RPC cast gets host supply back from all recepients
        through an RPC cast

        :param ctxt: Request context
        :type ctxt: nova.context.RequestContext
        :param json_supply: JSON-serialized HostSupply object
        :type json_supply: str
        """
        supply = cloud_supply.CloudSupply.HostSupply.from_json(json_supply)
        assert isinstance(supply, cloud_supply.CloudSupply.HostSupply),\
            "supply needs to be of type HostSupply"
        LOG.debug("Received host supply from host "+supply.compute_host+".")
        self._cloud_supply.add_supply(supply)
        # Answer the cast with a cast to the source if the source is online
        # and send it the local host supply information
        self._send_host_supply(ctxt.remote_address)
