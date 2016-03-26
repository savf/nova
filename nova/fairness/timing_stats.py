import csv
import time

from oslo.config import cfg

CONF = cfg.CONF
CONF.import_group('fairness', 'nova.fairness')


class TimingStats(object):

    def __init__(self):
        self._timestamps = dict()
        self._timestamps['rui_setup'] = dict()
        self._timestamps['rui'] = dict()
        self._timestamps['heaviness'] = dict()
        self._timestamps['reallocation_setup'] = dict()
        self._timestamps['cmd_reallocation'] = dict()
        self._timestamps['n_reallocation'] = dict()
        self._csv_path = '/var/log/nova/fairness/timing-stats-' +\
                         str(int(time.time())) + '.csv'
        with open(self._csv_path, 'w') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(('ITERATION_STEP', 'INSTANCE_NAME', 'TIME'))
            csv_file.close()
        self._instances = dict()

    def _write_timing(self, timing_type, instance_name, time_taken):
        """ Write all collected information for an instance

        :param timing_type: Timing type
        :type timing_type: str
        :param instance_name: Name of the instance
        :type instance_name: str
        :param time_taken: The amount of time taken in seconds
        :type time_taken: float
        """
        row = list()
        # ITERATION_STEP
        row.append(timing_type)
        # INSTANCE_NAME
        row.append(instance_name)
        # TIME
        row.append(time_taken)

        with open(self._csv_path, 'a') as csv_file:
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(row)
            csv_file.close()

    def start_timing(self, timing_type, instance_name="all"):
        """ Save start time for a specific instance

        :param timing_type: Timing type
        :type timing_type: str
        :param instance_name: Name of the instance
        :type instance_name: str
        """
        if CONF.fairness.timing_stats_enabled:
            self._timestamps[timing_type][instance_name] = time.time()

    def stop_timing(self, timing_type, instance_name="all"):
        """ Calculate time taken for a specific step

        :param timing_type: Timing type
        :type timing_type: str
        :param instance_name: Name of the instance
        :type instance_name: str
        """
        if CONF.fairness.timing_stats_enabled:
            current = time.time()
            if timing_type in self._timestamps:
                if instance_name in self._timestamps[timing_type]:
                    start_time = self._timestamps[timing_type][instance_name]
                    self._write_timing(timing_type, instance_name, current - start_time)
            self._timestamps[timing_type][instance_name] = None
