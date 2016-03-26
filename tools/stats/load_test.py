import csv
import libvirt
import os.path
import re
from subprocess import PIPE
from subprocess import call
from subprocess import check_output
from subprocess import Popen
import sys
import time
from xml.dom import minidom


csv_path = '/var/log/nova/fairness/'
experiment_duration = 240


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

    # find ip address of domain
    arp = check_output(['arp', '-an'])
    if arp is not None:
        m = re.search(r'\((.+)\) at ' + re.escape(mac), arp)

        if m:
            return m.group(1)
    else:
        # instance not ready;
        # <incomplete>, mac not found
        return False


def _write_results(interval, load, instances, services):
    filename = str(len(instances)) + "VM"
    with open(csv_path + filename, 'a') as csv_file:
        csv_writer = csv.writer(csv_file)
        row = [str(experiment_duration), str(interval), str(load)]
        for instance_name, instance in instances.iteritems():
            cpu_time = (int(instance['cpu_stop_time']) -
                        int(instance['cpu_start_time']))
            row.append(str(cpu_time))
        if 'nova-compute' in services:
            row.append(str(services['nova-compute']['cpu_stop_time'] -
                           services['nova-compute']['cpu_start_time']))
        if 'nova-network' in services:
            row.append(str(services['nova-network']['cpu_stop_time'] -
                           services['nova-network']['cpu_start_time']))
        if 'nova-api-metadata' in services:
            row.append(str(services['nova-api-metadata']['cpu_stop_time'] -
                       services['nova-api-metadata']['cpu_start_time']))
        if 'nova-fairness' in services:
            row.append(str(services['nova-fairness']['cpu_stop_time'] -
                           services['nova-fairness']['cpu_start_time']))
        csv_writer.writerow(row)
        csv_file.close()


def _get_cpu_time(pid):
    stats = check_output(['cat', '/proc/' + str(pid) + '/stat'])
    stats_array = stats.split(' ')
    return (float(stats_array[13]) + float(stats_array[14])) / 100


def main():
    conn = libvirt.open()
    domains = conn.listAllDomains()
    instances = dict()

    for domain in domains:
        instances[domain.name()] = dict()
        instances[domain.name()]['name'] = domain.name()
        pid = check_output(['cat', '/var/run/libvirt/qemu/' + domain.name() +
                            '.pid'])
        instances[domain.name()]['pid'] = pid
        ip = _find_domain_ip(domain)
        instances[domain.name()]['ip'] = ip
        instances[domain.name()]['running'] = False
        if domain.isActive():
            domain.shutdown()

    if not os.path.isfile(csv_path + str(len(instances)) + "VM"):
        with open(csv_path + str(len(instances)) + "VM", 'w') as csv_file:
            csv_writer = csv.writer(csv_file)
            row = ['EXPERIMENT_DURATION', 'INTERVAL_LENGTH', 'LOAD']
            for instance_name, instance in instances.iteritems():
                row.append(instance_name)
            row += ['NOVA_COMPUTE', 'NOVA_NETWORK', 'NOVA_API_METADATA',
                    'NOVA_FAIRNESS']
            csv_writer.writerow(row)

    # Get PIDs of nova services
    services = dict()
    ps = check_output(['ps', '-aux'])
    compute_match = re.search(
        r'nova\s*(\d+).*/usr/bin/python /usr/bin/nova-compute', ps)
    if compute_match:
        services['nova-compute'] = dict()
        services['nova-compute']['pid'] = int(compute_match.group(1))
    network_match = re.search(
        r'nova\s*(\d+).*/usr/bin/python /usr/bin/nova-network', ps)
    if network_match:
        services['nova-network'] = dict()
        services['nova-network']['pid'] = int(network_match.group(1))
    api_metadata_match = re.search(
        r'nova\s*(\d+).*/usr/bin/python /usr/bin/nova-api-metadata', ps)
    if api_metadata_match:
        services['nova-api-metadata'] = dict()
        services['nova-api-metadata']['pid'] = int(api_metadata_match.group(1))

    intervals = [-1, 1, 2, 4, 8, 16]
    active_instances = 0

    # wait for all vms to shut down
    time.sleep(30)

    for domain in domains:
        # start domain
        domain.create()
        active_instances += 1
        instances[domain.name]['running'] = True
        time.sleep(30)
        print "START EXPERIMENT WITH " + str(active_instances) + " VMs"
        for interval in intervals:
            sed_output = call(
                ["sed", "-i",
                 "s/rui_collection_interval=.*/rui_collection_interval=" +
                 str(interval) + "/g", "/etc/nova/nova.conf"],
                stderr=PIPE, stdout=PIPE)
            service_start_output = call(["service", "nova-fairness", "start"],
                                        stderr=PIPE, stdout=PIPE)

            ps = check_output(['ps', '-aux'])
            nova_fairness_pid = re.search(
                r'nova\s*(\d+).*/usr/bin/python /usr/bin/nova-fairness', ps)
            if nova_fairness_pid:
                if 'nova_fairness' not in services:
                    services['nova-fairness'] = dict()
                services['nova-fairness']['pid'] = int(
                    nova_fairness_pid.group(1))
            # Wait a minute for the nova-fairness service to fully initialize
            time.sleep(60)
            print "-- LOAD EXPERIMENT WITH INTERVAL " + str(interval)
            for load in [False, True]:
                for service_name, service in services.iteritems():
                    service['cpu_start_time'] = _get_cpu_time(service['pid'])
                for instance_name, instance in instances.iteritems():
                    instance['cpu_start_time'] = _get_cpu_time(instance['pid'])
                if load:
                    for instance_name, instance in instances.iteritems():
                        if instance['running']:
                            stress_output = Popen(["ssh", "-l", "ubuntu",
                                                   instance['ip'], "stress",
                                                   "--cpu", "2", "-t",
                                                   str(experiment_duration)],
                                                  stderr=PIPE, stdout=PIPE)
                    load_state = ""
                else:
                    load_state = "OUT"
                print "-- RUNNING " + str(experiment_duration) +\
                      " SECONDS WITH" + load_state + " LOAD"
                time.sleep(experiment_duration)
                for service_name, service in services.iteritems():
                    service['cpu_stop_time'] = _get_cpu_time(service['pid'])
                for instance_name, instance in instances.iteritems():
                    instance['cpu_stop_time'] = _get_cpu_time(instance['pid'])
                _write_results(interval, load, instances, services)
            service_stop_output = call(["service", "nova-fairness", "stop"],
                                       stderr=PIPE, stdout=PIPE)

if __name__ == '__main__':
    sys.exit(main())
