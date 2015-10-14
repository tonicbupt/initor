#!/usr/bin/env python
# coding: utf-8

import os
import array
import errno
import fcntl
import struct
import shutil
import socket
import subprocess
import tempfile
from datetime import datetime
from optparse import OptionParser


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def call_command(cmd):
    return subprocess.call(cmd.split(' '))


def make_dir(path):
    try:
        os.mkdir(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def make_file(path, content):
    with open(path, 'w') as f:
        f.write(content)
    os.chmod(path, 0644)


def all_interfaces():
    max_possible = 128  # arbitrary. raise if needed.
    bs = max_possible * 32
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    names = array.array('B', '\0' * bs)
    outbytes = struct.unpack('iL', fcntl.ioctl(
        s.fileno(),
        0x8912,  # SIOCGIFCONF
        struct.pack('iL', bs, names.buffer_info()[0])
    ))[0]
    namestr = names.tostring()
    r = {}

    def format_ip(addr):
        return '.'.join(str(ord(addr[i])) for i in range(4))

    for i in range(0, outbytes, 40):
        name = namestr[i:i+16].split('\0', 1)[0]
        ip = namestr[i+20:i+24]
        r[format_ip(ip)] = name
    return r


def get_interface_name(addr):
    interfaces = all_interfaces()
    return interfaces[addr]


def set_hostname(config):
    print '---> set host name to [%s] ...' % config.hostname
    call_command('hostnamectl set-hostname %s --static' % config.hostname)
    print '---> set host name to [%s] done' % config.hostname


def make_docker_config(config, storage=None):
    print '---> copy docker systemd file ...'
    shutil.copy('templates/docker.service', '/usr/lib/systemd/system/docker.service')
    print '---> copy docker systemd file done'

    # make docker run config
    print '---> make docker run config ...'
    with open('templates/etc.sysconfig.docker.tmpl') as f:
        tmpl = f.read()
        content = tmpl.format(**config)
        make_file('/etc/sysconfig/docker', content)
    print '---> make docker run config done'

    # docker storage config
    if storage:
        print '---> make docker storage config ...'
        with open('templates/etc.sysconfig.docker-storage.tmpl') as f:
            tmpl = f.read()
            content = tmpl.format(**storage)
            make_file('/etc/sysconfig/docker-storage', content)
        print '---> make docker storage config done'


def make_eru_agent_config(config):
    # copy eru-agent systemd file
    print '---> copy eru-agent systemd file ...'
    shutil.copy('templates/eru-agent.service', '/usr/lib/systemd/system/eru-agent.service')
    print '---> copy eru-agent systemd file done' 

    # make eru-agent run config
    print '---> make eru-agent run config ...'
    make_dir('/etc/eru-agent')
    with open('templates/agent.yaml.tmpl') as f:
        tmpl = f.read()
        content = tmpl.format(**config)
        make_file('/etc/eru-agent/agent.yaml', content)
    print '---> make eru-agent run config done'


DOCKER_SERVER_TLS_PATH = '/etc/docker/tls'
DOCKER_CLIENT_TLS_PATH = '/root/.docker'


def install_docker_agent(config):
    # install docker and eru-agent rpm package
    print '---> install docker / eru-agent rpm packages ...'
    call_command('yum -y install rpms/docker-latest.rpm')
    call_command('yum -y install rpms/eru-agent-latest.rpm')
    print '---> install docker / eru-agent rpm packages done' 

    # install nsenter and docker-enter tools
    print '---> install docker-enter / nsenter ...'
    shutil.copy2('bin/nsenter', '/usr/local/bin/nsenter')
    shutil.copy2('bin/docker-enter', '/usr/local/bin/docker-enter')
    print '---> install docker-enter / nsenter done' 

    # generate docker tls files
    print '---> generate docker tls certs ...'
    generate_certs(config)
    print '---> generate docker tls certs done'

    # generate docker and eru-agent config file
    docker_config = {
        'cacert': os.path.join(DOCKER_SERVER_TLS_PATH, 'ca.pem'),
        'cert': os.path.join(DOCKER_SERVER_TLS_PATH, 'server-cert.pem'),
        'key': os.path.join(DOCKER_SERVER_TLS_PATH, 'server-key.pem'),
        'registry': config.registry,
    }

    _metrics = (
        'metrics:\n'
        '  step: 30\n'
        '  timeout: 1000\n'
        '  transfers:\n'
    )
    for tran in config.transfer.split(','):
        _metrics += '    - %s:8433\n' % tran
    eru_agent_config = {
        'config': config,
        'physical': get_interface_name(config.ip),
        'metrics': _metrics,
    }

    make_docker_config(docker_config)
    make_eru_agent_config(eru_agent_config)


class TempSpace(object):
    def __init__(self):
        path = tempfile.mkdtemp()
        self.current_path = self.path = os.path.expanduser(path)

    def __enter__(self):
        self.current_path = os.getcwd()
        os.chdir(self.path)

    def __exit__(self, etype, value, tb):
        os.chdir(self.current_path)
        shutil.rmtree(self.path)


def generate_certs(config):
    builder = os.path.join(os.getcwd(), 'certs')
    with TempSpace():
        call_command('%s %s %s' % (builder, config.ip, os.getcwd()))

        # copy server side certs
        make_dir(os.path.dirname(DOCKER_SERVER_TLS_PATH))
        make_dir(DOCKER_SERVER_TLS_PATH)
        [shutil.copy(cert, os.path.join(DOCKER_SERVER_TLS_PATH, cert)) for cert in ('ca.pem', 'server-key.pem', 'server-cert.pem')]

        # copy client side certs
        make_dir(DOCKER_CLIENT_TLS_PATH)
        [shutil.copy(cert, os.path.join(DOCKER_CLIENT_TLS_PATH, cert)) for cert in ('ca.pem', 'key.pem', 'cert.pem')]

        # register to eru-core
        # TODO


def init_service():
    print '---> init services ...'
    call_command('systemctl enable docker.service')
    call_command('systemctl enable eru-agent.service')
    call_command('systemctl daemon-reload')
    call_command('systemctl start docker.service')
    call_command('systemctl start eru-agent.service')
    print '---> init services done'


def init_kernel():
    print '---> init kernel parameters ...'
    total_mem_kb = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / 1024
    max_tcp_mem = int(total_mem_kb * 0.9 / 4)
    mid_tcp_mem = int(total_mem_kb * 0.7 / 4)
    min_tcp_mem = int(total_mem_kb * 0.2 / 4)

    with open('templates/kernel.tmpl') as f:
        tmpl = f.read()
        content = tmpl.format(min_tcp_mem=min_tcp_mem, mid_tcp_mem=mid_tcp_mem, max_tcp_mem=max_tcp_mem)
        shutil.copy('/etc/sysctl.conf', '/etc/sysctl.conf.backup.%s' % now())
        make_file('/etc/sysctl.conf', content)

    call_command('sysctl -p')

    shutil.copy('/etc/security/limits.conf', '/etc/security/limits.conf.backup.%s' % now())
    shutil.copy('templates/ulimit.tmpl', '/etc/security/limits.conf')
    print '---> init kernel parameters done'


def parse_args():
    parser = OptionParser()
    parser.add_option('-i', '--ip', dest='ip', default='127.0.0.1')
    parser.add_option('-n', '--hostname', dest='hostname', default='localhost')
    parser.add_option('-e', '--endpoint', dest='endpoint', default='localhost')
    parser.add_option('-l', '--logstash', dest='logstash', default='localhost')
    parser.add_option('-c', '--lenz-count', dest='lenz_count', type='int', default=20)
    parser.add_option('-r', '--redis-host', dest='redis_host', default='localhost')
    parser.add_option('-p', '--redis-port', dest='redis_port', type='int', default=6379)
    parser.add_option('-g', '--registry', dest='registry', default='')
    parser.add_option('-t', '--transfer', dest='transfer', default='', help='use `,` to split multi ips')
    options, args = parser.parse_args()
    return options, args


def main(config):
    set_hostname(config)
    install_docker_agent(config)
    init_kernel()
    init_service()


if __name__ == '__main__':
    config, _ = parse_args()
    print '---> starting initializing ...'
    main(config)
    print '---> initializing done'
