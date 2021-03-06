#    Copyright 2016 Mirantis, Inc.
#
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

import random
import time

from devops.helpers import helpers
from devops.helpers import ssh_client
from paramiko import rsakey

from tcp_tests import logger
from tcp_tests.helpers import utils

LOG = logger.logger


class UnderlaySSHManager(object):
    """Keep the list of SSH access credentials to Underlay nodes.

       This object is initialized using config.underlay.ssh.

       :param config_ssh: JSONList of SSH access credentials for nodes:
          [
            {
              node_name: node1,
              address_pool: 'public-pool01',
              host: ,
              port: ,
              keys: [],
              keys_source_host: None,
              login: ,
              password: ,
              roles: [],
            },
            {
              node_name: node1,
              address_pool: 'private-pool01',
              host:
              port:
              keys: []
              keys_source_host: None,
              login:
              password:
              roles: [],
            },
            {
              node_name: node2,
              address_pool: 'public-pool01',
              keys_source_host: node1
              ...
            }
            ,
            ...
          ]

       self.node_names(): list of node names registered in underlay.
       self.remote(): SSHClient object by a node name (w/wo address pool)
                      or by a hostname.
    """
    __config = None
    config_ssh = None
    config_lvm = None

    def __init__(self, config):
        """Read config.underlay.ssh object

           :param config_ssh: dict
        """
        self.__config = config
        if self.config_ssh is None:
            self.config_ssh = []

        if self.config_lvm is None:
            self.config_lvm = {}

        self.add_config_ssh(self.__config.underlay.ssh)

    def add_config_ssh(self, config_ssh):

        if config_ssh is None:
            config_ssh = []

        for ssh in config_ssh:
            ssh_data = {
                # Required keys:
                'node_name': ssh['node_name'],
                'host': ssh['host'],
                'login': ssh['login'],
                'password': ssh['password'],
                # Optional keys:
                'address_pool': ssh.get('address_pool', None),
                'port': ssh.get('port', None),
                'keys': ssh.get('keys', []),
                'roles': ssh.get('roles', []),
            }

            if 'keys_source_host' in ssh:
                node_name = ssh['keys_source_host']
                remote = self.remote(node_name)
                keys = self.__get_keys(remote)
                ssh_data['keys'].extend(keys)

            self.config_ssh.append(ssh_data)

    def remove_config_ssh(self, config_ssh):
        if config_ssh is None:
            config_ssh = []

        for ssh in config_ssh:
            ssh_data = {
                # Required keys:
                'node_name': ssh['node_name'],
                'host': ssh['host'],
                'login': ssh['login'],
                'password': ssh['password'],
                # Optional keys:
                'address_pool': ssh.get('address_pool', None),
                'port': ssh.get('port', None),
                'keys': ssh.get('keys', []),
                'roles': ssh.get('roles', []),
            }
            self.config_ssh.remove(ssh_data)

    def __get_keys(self, remote):
        keys = []
        remote.execute('cd ~')
        key_string = './.ssh/id_rsa'
        if remote.exists(key_string):
            with remote.open(key_string) as f:
                keys.append(rsakey.RSAKey.from_private_key(f))
        return keys

    def __ssh_data(self, node_name=None, host=None, address_pool=None):

        ssh_data = None

        if host is not None:
            for ssh in self.config_ssh:
                if host == ssh['host']:
                    ssh_data = ssh
                    break

        elif node_name is not None:
            for ssh in self.config_ssh:
                if node_name == ssh['node_name']:
                    if address_pool is not None:
                        if address_pool == ssh['address_pool']:
                            ssh_data = ssh
                            break
                    else:
                        ssh_data = ssh
        if ssh_data is None:
            raise Exception('Auth data for node was not found using '
                            'node_name="{}" , host="{}" , address_pool="{}"'
                            .format(node_name, host, address_pool))
        return ssh_data

    def node_names(self):
        """Get list of node names registered in config.underlay.ssh"""

        names = []  # List is used to keep the original order of names
        for ssh in self.config_ssh:
            if ssh['node_name'] not in names:
                names.append(ssh['node_name'])
        return names

    def enable_lvm(self, lvmconfig):
        """Method for enabling lvm oh hosts in environment

        :param lvmconfig: dict with ids or device' names of lvm storage
        :raises: devops.error.DevopsCalledProcessError,
        devops.error.TimeoutError, AssertionError, ValueError
        """
        def get_actions(lvm_id):
            return [
                "systemctl enable lvm2-lvmetad.service",
                "systemctl enable lvm2-lvmetad.socket",
                "systemctl start lvm2-lvmetad.service",
                "systemctl start lvm2-lvmetad.socket",
                "pvcreate {} && pvs".format(lvm_id),
                "vgcreate default {} && vgs".format(lvm_id),
                "lvcreate -L 1G -T default/pool && lvs",
            ]
        lvmpackages = ["lvm2", "liblvm2-dev", "thin-provisioning-tools"]
        for node_name in self.node_names():
            lvm = lvmconfig.get(node_name, None)
            if not lvm:
                continue
            if 'id' in lvm:
                lvmdevice = '/dev/disk/by-id/{}'.format(lvm['id'])
            elif 'device' in lvm:
                lvmdevice = '/dev/{}'.format(lvm['device'])
            else:
                raise ValueError("Unknown LVM device type")
            if lvmdevice:
                self.apt_install_package(
                    packages=lvmpackages, node_name=node_name, verbose=True)
                for command in get_actions(lvmdevice):
                    self.sudo_check_call(command, node_name=node_name,
                                         verbose=True)
        self.config_lvm = dict(lvmconfig)

    def host_by_node_name(self, node_name, address_pool=None):
        ssh_data = self.__ssh_data(node_name=node_name,
                                   address_pool=address_pool)
        return ssh_data['host']

    def remote(self, node_name=None, host=None, address_pool=None):
        """Get SSHClient by a node name or hostname.

           One of the following arguments should be specified:
           - host (str): IP address or hostname. If specified, 'node_name' is
                         ignored.
           - node_name (str): Name of the node stored to config.underlay.ssh
           - address_pool (str): optional for node_name.
                                 If None, use the first matched node_name.
        """
        ssh_data = self.__ssh_data(node_name=node_name, host=host,
                                   address_pool=address_pool)
        return ssh_client.SSHClient(
            host=ssh_data['host'],
            port=ssh_data['port'] or 22,
            username=ssh_data['login'],
            password=ssh_data['password'],
            private_keys=ssh_data['keys'])

    def check_call(
            self, cmd,
            node_name=None, host=None, address_pool=None,
            verbose=False, timeout=None,
            error_info=None,
            expected=None, raise_on_err=True):
        """Execute command on the node_name/host and check for exit code

        :type cmd: str
        :type node_name: str
        :type host: str
        :type verbose: bool
        :type timeout: int
        :type error_info: str
        :type expected: list
        :type raise_on_err: bool
        :rtype: list stdout
        :raises: devops.error.DevopsCalledProcessError
        """
        remote = self.remote(node_name=node_name, host=host,
                             address_pool=address_pool)
        return remote.check_call(
            command=cmd, verbose=verbose, timeout=timeout,
            error_info=error_info, expected=expected,
            raise_on_err=raise_on_err)

    def apt_install_package(self, packages=None, node_name=None, host=None,
                            **kwargs):
        """Method to install packages on ubuntu nodes

        :type packages: list
        :type node_name: str
        :type host: str
        :raises: devops.error.DevopsCalledProcessError,
        devops.error.TimeoutError, AssertionError, ValueError

        Other params of check_call and sudo_check_call are allowed
        """
        expected = kwargs.pop('expected', None)
        if not packages or not isinstance(packages, list):
            raise ValueError("packages list should be provided!")
        install = "apt-get install -y {}".format(" ".join(packages))
        # Should wait until other 'apt' jobs are finished
        pgrep_expected = [0, 1]
        pgrep_command = "pgrep -a -f apt"
        helpers.wait(
            lambda: (self.check_call(
                pgrep_command, expected=pgrep_expected, host=host,
                node_name=node_name, **kwargs).exit_code == 1
            ), interval=30, timeout=1200,
            timeout_msg="Timeout reached while waiting for apt lock"
        )
        # Install packages
        self.sudo_check_call("apt-get update", node_name=node_name, host=host,
                             **kwargs)
        self.sudo_check_call(install, expected=expected, node_name=node_name,
                             host=host, **kwargs)

    def sudo_check_call(
            self, cmd,
            node_name=None, host=None, address_pool=None,
            verbose=False, timeout=None,
            error_info=None,
            expected=None, raise_on_err=True):
        """Execute command with sudo on node_name/host and check for exit code

        :type cmd: str
        :type node_name: str
        :type host: str
        :type verbose: bool
        :type timeout: int
        :type error_info: str
        :type expected: list
        :type raise_on_err: bool
        :rtype: list stdout
        :raises: devops.error.DevopsCalledProcessError
        """
        remote = self.remote(node_name=node_name, host=host,
                             address_pool=address_pool)
        with remote.get_sudo(remote):
            return remote.check_call(
                command=cmd, verbose=verbose, timeout=timeout,
                error_info=error_info, expected=expected,
                raise_on_err=raise_on_err)

    def dir_upload(self, host, source, destination):
        """Upload local directory content to remote host

        :param host: str, remote node name
        :param source: str, local directory path
        :param destination: str, local directory path
        """
        with self.remote(node_name=host) as remote:
            remote.upload(source, destination)

    def get_random_node(self):
        """Get random node name

        :return: str, name of node
        """
        return random.choice(self.node_names())

    def yaml_editor(self, file_path, node_name=None, host=None,
                    address_pool=None):
        """Returns an initialized YamlEditor instance for context manager

        Usage (with 'underlay' fixture):

        # Local YAML file
        with underlay.yaml_editor('/path/to/file') as editor:
            editor.content[key] = "value"

        # Remote YAML file on TCP host
        with underlay.yaml_editor('/path/to/file',
                                  host=config.tcp.tcp_host) as editor:
            editor.content[key] = "value"
        """
        # Local YAML file
        if node_name is None and host is None:
            return utils.YamlEditor(file_path=file_path)

        # Remote YAML file
        ssh_data = self.__ssh_data(node_name=node_name, host=host,
                                   address_pool=address_pool)
        return utils.YamlEditor(
            file_path=file_path,
            host=ssh_data['host'],
            port=ssh_data['port'] or 22,
            username=ssh_data['login'],
            password=ssh_data['password'],
            private_keys=ssh_data['keys'])

    def ensure_running_service(self, service_name, host, check_cmd,
                               state_running='start/running'):
        """Check if the service_name running or try to restart it

        :param service_name: name of the service that will be checked
        :param node_name: node on which the service will be checked
        :param check_cmd: shell command to ensure that the service is running
        :param state_running: string for check the service state
        """
        cmd = "service {0} status | grep -q '{1}'".format(
            service_name, state_running)
        with self.remote(host=host) as remote:
            result = remote.execute(cmd)
            if result.exit_code != 0:
                LOG.info("{0} is not in running state on the node {1},"
                         " trying to start".format(service_name, host))
                cmd = ("service {0} stop;"
                       " sleep 3; killall -9 {0};"
                       "service {0} start; sleep 5;"
                       .format(service_name))
                remote.execute(cmd)

                remote.execute(check_cmd)
                remote.execute(check_cmd)

    def execute_commands(self, commands, label="Command"):
        """Execute a sequence of commands

        Main propose is to implement workarounds for salt formulas like:
        - exit_code == 0 when there are actual failures
        - salt_master and/or salt_minion stop working after executing a formula
        - a formula fails at first run, but completes at next runs

        :param label: label of the current sequence of the commands, for log
        :param commands: list of dicts with the following data:
        commands = [
            ...
            {
                # Required:
                'cmd': 'shell command(s) to run',
                'node_name': 'name of the node to run the command(s)',
                # Optional:
                'description': 'string with a readable command description',
                'retry': {
                    'count': int,  # How many times should be run the command
                                   # until success
                    'delay': int,  # Delay between tries in seconds
                },
                'skip_fail': bool  # If True - continue with the next step
                                   # without failure even if count number
                                   # is reached.
                                   # If False - rise an exception (default)
            },
            ...
        ]
        """
        for n, step in enumerate(commands):
            # Required fields
            cmd = step.get('cmd')
            node_name = step.get('node_name')
            # Optional fields
            description = step.get('description', cmd)
            retry = step.get('retry', {'count': 1, 'delay': 1})
            retry_count = retry.get('count', 1)
            retry_delay = retry.get('delay', 1)
            skip_fail = step.get('skip_fail', False)

            msg = "[ {0} #{1} ] {2}".format(label, n+1, description)
            LOG.info("\n\n{0}\n{1}".format(msg, '=' * len(msg)))

            with self.remote(node_name=node_name) as remote:

                for x in range(retry_count, 0, -1):
                    time.sleep(3)
                    result = remote.execute(cmd, verbose=True)

                    # Workaround of exit code 0 from salt in case of failures
                    failed = 0
                    for s in result['stdout']:
                        if s.startswith("Failed:"):
                            failed += int(s.split("Failed:")[1])

                    if result.exit_code != 0:
                        time.sleep(retry_delay)
                        LOG.info(" === RETRY ({0}/{1}) ========================="
                                 .format(x-1, retry_count))
                    elif failed != 0:
                        LOG.error(" === SALT returned exit code = 0 while "
                                  "there are failed modules! ===")
                        LOG.info(" === RETRY ({0}/{1}) ======================="
                                 .format(x-1, retry_count))
                    else:
                        if self.__config.salt.salt_master_host != '0.0.0.0':
                            # Workarounds for crashed services
                            self.ensure_running_service(
                                "salt-master",
                                self.__config.salt.salt_master_host,
                                "salt-call pillar.items",
                                'active (running)') # Hardcoded for now
                            self.ensure_running_service(
                                "salt-minion",
                                self.__config.salt.salt_master_host,
                                "salt 'cfg01*' pillar.items",
                                "active (running)") # Hardcoded for now
                            break

                    if x == 1 and skip_fail == False:
                        # In the last retry iteration, raise an exception
                        raise Exception("Step '{0}' failed"
                                        .format(description))
