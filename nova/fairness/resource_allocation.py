import binascii
import re
import socket

from oslo.config import cfg
from xml.dom import minidom

from nova import utils
from nova.openstack.common import log as logging
from nova.openstack.common import processutils
from nova.virt import driver
from nova.virt import virtapi

resource_allocation_opts = [
    cfg.IntOpt('htb_rate',
               default='100',
               help='Maximum network throughput rate in MBit/s per instance to'
                    'use as ceiling for the qdisc htb network prioritization.')
    ]

CONF = cfg.CONF
fairness_group = cfg.OptGroup("fairness", "Fairness configuration options")
CONF.import_opt('host', 'nova.netconf')
CONF.register_group(fairness_group)
CONF.register_opts(resource_allocation_opts, fairness_group)
LOG = logging.getLogger(__name__)


class ResourceAllocation(object):

    def __init__(self, fairness_heavinesses, rui_statistics,
                 timing_statistics, fairness_quota, global_norm):
        self._fairness_heavinesses = fairness_heavinesses
        self._local_heavinesses = None
        self._user_count = None
        self.host = CONF.host
        self.bridge_interface = self._find_bridge_interface()
        self._rui_stats = rui_statistics
        self._timing_stats = timing_statistics
        self._fairness_quota = fairness_quota
        self._global_norm = global_norm
        self.driver = driver.load_compute_driver(virtapi.VirtAPI,
                                                 'libvirt.LibvirtDriver')

    @property
    def user_count(self):
        return self._user_count

    @staticmethod
    def _subtract_residual_quota(quota_sum, user_heavinesses, user_endowments):
        """ Subtract the residual quota from the combined user heaviness

        Subtract the sum of all elements of the normalized endowments
        of all of the user's instances from the quota sum to get the
        'residual quota'. Then subtract this residual quota from the sum
        of all heavinesses of the user's instances

        :param quota_sum: Sum of elements of normalized quota
        :type quota_sum: int
        :param user_heavinesses: Sum of all heavinesses of the user's instances
        :type user_heavinesses: int
        :param user_endowments: Normalized endowments
        :type user_endowments: int
        :return: User heavinesses without residual quota
        :rtype: int
        """
        residual_quota = quota_sum - user_endowments
        return user_heavinesses - residual_quota

    def _calculate_local_heavinesses(self):
        """ Calculate heavinesses for all instances running on the compute host

        The heavinesses of each user are added to the heaviness of an instance
        to result in a user-centric heaviness for each instance:
        heaviness(instance) = heaviness(User) + heaviness(instance)
        """
        users_needed = set()
        remaining_users = set()
        user_heavinesses = dict()
        user_endowments = dict()
        if self.host in self._fairness_heavinesses:
            self._local_heavinesses =\
                self._fairness_heavinesses[self.host].get()
            for instance_name,\
                    instance_info in self._local_heavinesses.iteritems():
                users_needed.add(instance_info['user_id'])
            for host, queue in self._fairness_heavinesses.iteritems():
                if queue.qsize() > 0 or host == self.host:
                    if host == self.host:
                        heavinesses = self._local_heavinesses
                    else:
                        heavinesses = queue.get()
                    for instance_name, instance_info in heavinesses.iteritems():
                        if instance_info['user_id'] in users_needed:
                            if instance_info['user_id'] in user_heavinesses:
                                user_heavinesses[instance_info['user_id']] +=\
                                    instance_info['heaviness']
                            else:
                                user_heavinesses[instance_info['user_id']] =\
                                    instance_info['heaviness']
                            if instance_info['user_id'] in user_endowments:
                                user_endowments[instance_info['user_id']] +=\
                                    instance_info['normalized_endowment']
                            else:
                                user_endowments[instance_info['user_id']] =\
                                    instance_info['normalized_endowment']
                        else:
                            remaining_users.add(instance_info['user_id'])
            # Normalize the quota and sum up all elements once and
            # use it for all users
            quota = self._fairness_quota * self._global_norm
            quota_sum = (quota.cpu_time +
                         quota.disk_bytes_read +
                         quota.disk_bytes_written +
                         quota.network_bytes_received +
                         quota.network_bytes_transmitted +
                         quota.memory_used)
            for user_id in users_needed:
                user_heavinesses[user_id] = self._subtract_residual_quota(
                        quota_sum,
                        user_heavinesses[user_id],
                        user_endowments[user_id])
            for instance_name,\
                    instance_info in self._local_heavinesses.iteritems():
                instance_info['heaviness'] =\
                    (user_heavinesses[instance_info['user_id']] +
                     instance_info['heaviness'])
            self._user_count = len(users_needed) + len(remaining_users)

    def _heaviness_to_priority(self, instance_name, heaviness):
        """ Convert the heaviness of an instance into a priority

        The range for the priority is [-50,50] with -50 being the highest
        priority and 50 being the lowest priority. The conversion takes into
        account that heavinesses < 0 should be prioritized higher by a factor
        of 2.5 compared to heavinesses >= 0

        :param instance_name: Name of the instance
        :type instance_name: str
        :param heaviness: Heaviness to convert from
        :type heaviness: float
        :return: Converted priority
        :rtype: int
        """
        if heaviness >= 0:
            priority = heaviness * 50
            if priority > 50:
                priority = 50
        else:
            priority = heaviness * 50 * 2.5
            if priority < -50:
                priority = -50
        # Eliminate harsh jumps in priorities compared to old priorities
        if 'last_priority' in self._local_heavinesses[instance_name]:
            last_priority =\
                self._local_heavinesses[instance_name]['last_priority']
            self._local_heavinesses[instance_name]['last_priority'] = priority
            difference = abs(priority - last_priority) * 0.1
            if priority < last_priority:
                priority = last_priority - difference
            else:
                priority = last_priority + difference

        return int(priority)

    @staticmethod
    def _convert_priority_range(priority, new_minimum, new_maximum):
        """ Convert a priority from the [-50:50] range to a new range.

        The method makes sure that the output will never exceed or underrun
        the new range even if the input-priority is out of range in relation
        to the old range

        :param priority: Priority to convert
        :type priority: int
        :param new_minimum: The minimum value of the new range
        :type new_minimum: int
        :param new_maximum: The maximum value of the new range
        :type new_maximum: int
        :return: Converted priority
        :rtype: int
        """
        new_range = new_maximum - new_minimum
        new_priority = (((-priority+50)*new_range)/100) + new_minimum
        if new_priority < new_minimum:
            return new_minimum
        if new_priority > new_maximum:
            return new_maximum
        return new_priority

    def _write_stats(self, instance_name, instance_heaviness):
        """ Save the applied priorities to a stats file

        The stats file is managed by the nova.fairness.rui_stats.RUIStats class.
        Saving stats can be enabled and disabled with the 'rui_stats_enabled'
        configuration entry in the group 'fairness' of the nova.conf
        configuration file

        :param instance_name: Instance to which priorities apply
        :type instance_name: str
        """
        if CONF.fairness.rui_stats_enabled:
            cpu_shares = self.get_cpu_shares(instance_name)
            memory_soft_limit = self.get_memory_soft_limit(instance_name)
            disk_weight = self.get_disk_weight(instance_name)
            net_priority = self.get_net_priority(instance_name)
            self._rui_stats.add_prioritization(instance_name,
                                               instance_heaviness, cpu_shares,
                                               memory_soft_limit, disk_weight,
                                               net_priority)

    def reallocate(self):
        """ Apply priorities for all stored instance heavinesses """
        self._timing_stats.start_timing("reallocation_setup")
        self._calculate_local_heavinesses()
        self._timing_stats.stop_timing("reallocation_setup")
        if self._local_heavinesses is not None:
            for instance_name,\
                    instance_info in self._local_heavinesses.iteritems():
                self._timing_stats.start_timing("cmd_reallocation", instance_name)
                priority = self._heaviness_to_priority(instance_name,
                                                  instance_info['heaviness'])
                LOG.debug(str(instance_name) +
                          ": Heaviness: " + str(instance_info['heaviness']) +
                          " -> Priority: " + str(priority))
                self._set_cpu_priority(instance_name, priority)
                self._set_memory_priority(instance_name, priority)
                self._set_disk_priority(instance_name, priority)
                self._timing_stats.stop_timing("cmd_reallocation", instance_name)
                self._timing_stats.start_timing("n_reallocation", instance_name)
                self._set_net_priority(instance_name, priority)
                self._timing_stats.stop_timing("n_reallocation", instance_name)

                self._write_stats(instance_name, instance_info['heaviness'])

    def get_cpu_shares(self, instance_name):
        """Get the current CPU shares of an instance

        :param instance_name: Name in the form 'instance-0000001a'
        :type instance_name: str
        """

        domain = self.driver._lookup_by_name(instance_name)

        return domain.schedulerParameters()['cpu_shares']

    def _set_cpu_priority(self, instance_name, priority):
        """Sets the cpu share priority of an instance

        The CPU with the highest shares value gets most resources

        :param instance_name: Name in the form 'instance-0000001a'
        :type instance_name: str
        :param priority: Priority for the CPU in the range [-50,50] with -50
                         as highest and 50 as lowest priority
        :type priority: int
        """

        domain = self.driver._lookup_by_name(instance_name)
        cpu_shares = self._convert_priority_range(priority, 1, 100)

        params = domain.schedulerParameters()
        params['cpu_shares'] = long(cpu_shares)

        result = domain.setSchedulerParameters(params)

        return not result

    def get_memory_soft_limit(self, instance_name):
        """Get the current memory soft limit of an instance

        :param instance_name: Name in the form 'instance-0000001a'
        :type instance_name: str
        """

        domain = self.driver._lookup_by_name(instance_name)

        return domain.memoryParameters()['soft_limit']

    def _set_memory_priority(self, instance_name, priority):
        """Set RAM soft limit of an instance

        It is assured that a minimum soft limit of 10 MiB is guaranteed

        :param instance_name: Name in the form 'instance-0000001a'
        :type instance_name: str
        :param priority: RAM priority in the range [-50,50] with -50
                         as highest and 50 as lowest priority
        :type priority: int
        """

        domain = self.driver._lookup_by_name(instance_name)
        total_memory = domain.maxMemory()
        softlimit = self._convert_priority_range(priority, 10240, total_memory)
        result = domain.setMemoryParameters(
                {'soft_limit': int(softlimit)})

        # libvirt returns 0 if successful
        return not result

    def get_disk_weight(self, instance_name):
        """Get the current I/O weight of an instance

        :param instance_name: Name in the form 'instance-0000001a'
        :type instance_name: str
        """

        domain = self.driver._lookup_by_name(instance_name)

        return domain.blkioParameters()['weight']

    def _set_disk_priority(self, instance_name, priority):
        """Sets the I/O weight of an instance

        The weight is in the range [100,1000]

        :param instance_name: Name in the form 'instance-0000001a'
        :type instance_name: str
        :param priority: I/O priority in the range [-50,50] with -50
                         as highest and 50 as lowest priority
        :type priority: int
        """
        domain = self.driver._lookup_by_name(instance_name)

        io_weight = self._convert_priority_range(priority, 100, 1000)
        params = domain.blkioParameters()
        params['weight'] = io_weight
        result = domain.setBlkioParameters(params)

        return not result

    def get_net_priority(self, instance_name):
        """Returns the current network priority of an instance

        :param instance_name: Name in the form 'instance-0000001a'
        :type instance_name: str
        """

        domain = self.driver._lookup_by_name(instance_name)

        ip = self._find_domain_ip(domain)

        # find the IP of the instance in the filter output
        # print the class minor, which is the same as the prio
        filters, error = utils.execute('tc', 'filter', 'list', 'dev',
                                       self.bridge_interface, run_as_root=True)
        prio = -1
        if filters is not None:
            hex_ip = binascii.hexlify(socket.inet_aton(ip))
            if hex_ip is not None:
                m = re.search(r'flowid\s1:(\d+)\s*match\s' +
                              hex_ip +
                              '/ffffffff\sat', filters)
                if m:
                    prio = m.group(1)

        return prio

    def _set_net_priority(self, instance_name, priority):
        """Sets the network priority of an instance

        Example for three hosts:
            tc qdisc add dev eth0 root handle 1: htb
            tc class add dev eth0 parent 1: classid 1:99 htb rate 80mbit
            tc class add dev eth0 parent 1:99 classid 1:3 htb rate 15mbit \
             ceil 80mbit
            tc class add dev eth0 parent 1:99 classid 1:2 htb rate 26mbit ceil \
             80mbit
            tc class add dev eth0 parent 1:99 classid 1:1 htb rate 39mbit \
             ceil 80mbit
            tc filter add dev eth0 parent 1: protocol ip prio 1 \
             u32 match ip src 10.0.0.4 flowid 1:3
            tc filter add dev eth0 parent 1: protocol ip prio 1 u32 \
             match ip src 10.0.0.3 flowid 1:2
            tc filter add dev eth0 parent 1: protocol ip prio 1 u32 \
             match ip src 10.0.0.2 flowid 1:1

        The priority gets translated into a htb qdisc class from 1:0 to 1:98
        where 1:98 is the highest and 1:0 the lowest priority

        :param instance_name: Name in the form 'instance-0000001a'
        :type instance_name: str
        :param priority: Network priority in the range [-50,50] with -50
                         as highest priority and 50 as lowest priority
        :type priority: int
        """

        domain = self.driver._lookup_by_name(instance_name)
        domains = self.driver._list_instance_domains()
        ips = [self._find_domain_ip(d) for d in domains]
        domain_ip = self._find_domain_ip(domain)
        priority = self._convert_priority_range(priority, 1, 98)
        result = True

        # find the IP of the instance in the filter output
        # the flowid is the class name, which is the same as
        # the network priority of the instance
        prios = {}
        output, error = utils.execute('tc', 'filter', 'list', 'dev',
                                      self.bridge_interface, run_as_root=True)
        if output is not None:
            for ip in ips:
                if ip and isinstance(ip, str):
                    m = re.search(r'flowid\s1:(\d+)\s*match\s' +
                                  binascii.hexlify(socket.inet_aton(ip)) +
                                  '/ffffffff\sat', output)
                    if m:
                        prio = int(m.group(1))
                        # only add priorities of other instances
                        if not domain_ip == ip:
                            if prio not in prios:
                                prios[prio] = set()
                            prios[prio].add(ip)

        LOG.debug('Current prios: %s', prios)

        # create new prio hash
        if priority not in prios:
            prios[priority] = set()
        prios[priority].add(domain_ip)
        active_prios = len({v for vv in prios.values() for v in vv})

        LOG.debug('New prios: %s', prios)
        LOG.debug('Priorities active for %d domains', active_prios)

        # reset egress tc qdiscs
        try:
            utils.execute('tc', 'qdisc', 'del', 'dev', self.bridge_interface,
                          'root', run_as_root=True)
        except processutils.ProcessExecutionError:
            pass

        # add root htb qdisc with CONF.tc.htb_rate
        # for total host throughput
        try:
            utils.execute('tc', 'qdisc', 'add', 'dev', self.bridge_interface,
                          'root', 'handle', '1:', 'htb', run_as_root=True)
        except processutils.ProcessExecutionError:
            result = False

        # add root class to enable htb borrowing
        try:
            utils.execute('tc', 'class', 'add', 'dev', self.bridge_interface,
                          'parent', '1:', 'classid', '1:99', 'htb', 'rate',
                          '%dmbit' % CONF.fairness.htb_rate, run_as_root=True)
        except processutils.ProcessExecutionError:
            result = False

        prio_sum = 0
        for prio in prios:
            prio_sum += prio

        # create child classes
        for prio in sorted(prios):
            classid = '1:%s' % prio

            try:
                utils.execute('tc', 'class', 'add', 'dev',
                              self.bridge_interface, 'parent', '1:99',
                              'classid', classid, 'htb', 'rate',
                              '%dmbit' % ((CONF.fairness.htb_rate * prio) /
                                          prio_sum),
                              'ceil', '%dmbit' % CONF.fairness.htb_rate,
                              run_as_root=True)
            except processutils.ProcessExecutionError:
                result = False

            for ip in prios[prio]:
                # add filters
                try:
                    utils.execute('tc', 'filter', 'add', 'dev',
                                  self.bridge_interface, 'parent', '1:',
                                  'protocol', 'ip', 'prio', '1', 'u32', 'match',
                                  'ip', 'src', ip, 'flowid', classid,
                                  run_as_root=True)
                except processutils.ProcessExecutionError:
                    result = False

        return result

    @staticmethod
    def _find_bridge_interface():
        """ Find virtual ethernet interface of the software bridge br100 """

        brctl, error = utils.execute('brctl', 'show', run_as_root=True)
        iface = None
        if brctl is not None:
            m = re.search(r'br100\s+.+?\s+(yes|no)\s+(eth\d)', brctl)
            if m:
                iface = m.group(2)

        return iface

    @staticmethod
    def _find_domain_ip(domain):
        """ Find IP address of libvirt domain

        :param domain: The instance domain
        :type domain: libvirt.virDomain
        """

        # find mac address of domain
        out = domain.XMLDesc()
        xml_desc = minidom.parseString(out)
        mac = xml_desc.getElementsByTagName('mac')[0]\
            .attributes['address'].value

        LOG.debug('Mac: %s', mac)

        # find ip address of domain
        arp, error = utils.execute('arp', '-an', run_as_root=True)
        if arp is not None:
            m = re.search(r'\((.+)\) at ' + re.escape(mac), arp)

            if m:
                return m.group(1)
        else:
            # instance not ready;
            # <incomplete>, mac not found
            LOG.debug('Instance not ready, mac not found.')
            return False
