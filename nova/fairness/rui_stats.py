import csv
import time

from oslo.config import cfg

from nova.fairness import metrics

CONF = cfg.CONF
CONF.import_group('fairness', 'nova.fairness')


class RUIStats(object):

    def __init__(self):
        self._csv_path = '/var/log/nova/fairness/rui-stats.csv'
        with open(self._csv_path, 'w') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(('TIMESTAMP', 'INSTANCE', 'HEAVINESS',
                                 'CPU_SHARES', 'CPU_USAGE',
                                 'MEMORY_SOFT_LIMIT', 'MEMORY_USED',
                                 'DISK_WEIGHT', 'DISK_BYTES_TRANSFERRED',
                                 'NET_PRIORITY', 'NET_BYTES_TRANSFERRED'))
            csv_file.close()
        self._instances = dict()

    def _write_complete_instance(self, instance_name):
        """ Write all collected information for an instance

        :param instance_name: Name of the instance
        :type instance_name: str
        """
        if 'rui' in self._instances[instance_name] and\
                'prioritization' in self._instances[instance_name]:
            row = list()
            rui = self._instances[instance_name]['rui']
            interval = self._instances[instance_name]['interval']
            prioritization =\
                self._instances[instance_name]['prioritization']
            assert isinstance(rui, metrics.BaseMetric.ResourceInformation),\
                "RUI has to be of type ResourceInformation"
            # Timestamp
            row.append(int(time.time()))
            # INSTANCE
            row.append(instance_name)
            # HEAVINESS
            row.append(prioritization['heaviness'])
            # CPU
            row.append(prioritization['cpu_shares'])
            cpu_load = int((100 * (rui.cpu_time/5999)) / interval)
            row.append(cpu_load)
            # MEMORY
            row.append(prioritization['memory_soft_limit'])
            row.append(rui.memory_used)
            # DISK
            row.append(prioritization['disk_weight'])
            row.append(rui.disk_bytes_written +
                       rui.disk_bytes_read)
            # NET
            row.append(int(prioritization['net_priority']))
            row.append(rui.network_bytes_transmitted +
                       rui.network_bytes_received)

            with open(self._csv_path, 'a') as csv_file:
                csv_writer = csv.writer(csv_file)
                csv_writer.writerow(row)
                csv_file.close()
            del self._instances[instance_name]['rui']
            del self._instances[instance_name]['prioritization']
            del self._instances[instance_name]['interval']

    def add_rui(self, rui, interval):
        """ Add RUI information for an instance

        :param rui: RUI of an instance
        :type rui: nova.fairness.metrics.BaseMetric.ResourceInformation
        :param interval: Interval of the RUI collection task
        :type interval: int
        """
        if CONF.fairness.rui_stats_enabled:
            assert isinstance(rui, metrics.BaseMetric.ResourceInformation),\
                "RUI has to be of type ResourceInformation"
            instance_name = rui.instance_name
            if instance_name not in self._instances:
                self._instances[instance_name] = dict()
            self._instances[instance_name]['rui'] = rui
            self._instances[instance_name]['interval'] = interval
            self._write_complete_instance(instance_name)

    def add_prioritization(self, instance_name, heaviness, cpu_shares,
                           memory_soft_limit, disk_weight, net_priority):
        """ Add priorities for an instance

        :param instance_name: Name of the instance
        :type instance_name: str
        :param heaviness: Heaviness for the instance
        :type heaviness: float
        :param cpu_shares: CPU shares applied to the instance
        :type cpu_shares: int
        :param memory_soft_limit: Memory soft-limit applied to the instance
        :type memory_soft_limit: int
        :param disk_weight: Disk weight applied to the instance
        :type disk_weight: int
        :param net_priority: Network priority applied to the instance
        :type net_priority: int
        """
        if CONF.fairness.rui_stats_enabled:
            if instance_name not in self._instances:
                self._instances[instance_name] = dict()
            prioritization = dict()
            prioritization['heaviness'] = heaviness
            prioritization['cpu_shares'] = cpu_shares
            prioritization['memory_soft_limit'] = memory_soft_limit
            prioritization['disk_weight'] = disk_weight
            prioritization['net_priority'] = net_priority
            self._instances[instance_name]['prioritization'] = prioritization
            self._write_complete_instance(instance_name)
