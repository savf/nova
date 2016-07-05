OpenStack Nova with nova-fairness
=====================

OpenStack Nova provides a cloud computing fabric controller,
supporting a wide variety of virtualization technologies,
including KVM, Xen, LXC, VMware, and more. In addition to
its native API, it includes compatibility with the commonly
encountered Amazon EC2 and S3 APIs.

The nova-fairness service adds a resource reallocation capability
to nova compute that allows it to dynamically enforce fairness
among cloud users during VM runtime by collecting runtime
usage information of all VMs and calculating a resulting heaviness
per user which is in turn used to prioritize users with lighter
workloads by setting CPU shares, RAM soft-limits, disk weights and
network priorities in their favor.

To set up a development environment to work on the code in
this repository, follow the installations steps outlined below:

### Mac OS X

Clone the repository:

```
git clone https://github.com/savf/novafairness.git
```

Install *virtualenv* then run the *install_venv.py* Python script under *tools*:

```
sudo easy_install virtualenv
python novafairness/tools/install_venv.py
```

To use the *virtualenv* in PyCharm, go to PyCharm -> Preferences -> Project -> Project Interpreter
and click on the cog. Choose "Add Local" and navigate to *novafairness/.venv/bin/python2.7*.
