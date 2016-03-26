import libvirt
import re
from subprocess import PIPE
from subprocess import call
from subprocess import check_output
from subprocess import Popen
import sys
import time
from xml.dom import minidom


experiment_duration = 300


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


def main():
    params = sys.argv
    if len(params) <= 1:
        print "Usage: python runtime_test.py LOAD_BOOLEAN"
        exit(1)
    load = bool(params[1])
    conn = libvirt.open()
    domains = conn.listAllDomains()
    instances = dict()

    for domain in domains:
        instances[domain.name()] = dict()
        instances[domain.name()]['name'] = domain.name()
        ip = _find_domain_ip(domain)
        instances[domain.name()]['ip'] = ip
        instances[domain.name()]['running'] = False
        if domain.isActive():
            domain.shutdown()

    intervals = [10]
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
            sed_interval_output = call(
                ["sed", "-i",
                 "s/rui_collection_interval=.*/rui_collection_interval=" +
                 str(interval) + "/g", "/etc/nova/nova.conf"],
                stderr=PIPE, stdout=PIPE)
            sed_timingstats_output = call(
                ["sed", "-i",
                 "s/timing_stats_enabled=.*/timing_stats_enabled=True/g",
                 "/etc/nova/nova.conf"],
                stderr=PIPE, stdout=PIPE)
            service_start_output = call(["service", "nova-fairness", "start"],
                                        stderr=PIPE, stdout=PIPE)

            print "-- RUNTIME EXPERIMENT WITH INTERVAL " + str(interval) +\
                  " SECONDS"
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
            print "-- RUNNING " + str(experiment_duration) + " SECONDS WITH" +\
                  load_state + " LOAD"
            time.sleep(experiment_duration)
            service_stop_output = call(["service", "nova-fairness", "stop"],
                                       stderr=PIPE, stdout=PIPE)

if __name__ == '__main__':
    sys.exit(main())
