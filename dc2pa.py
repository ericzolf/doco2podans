#!/usr/bin/env python
import jinja2
import sys
import yaml

# names of podman ansible modules

PODMAN_VOLUME = 'containers.podman.podman_volume'
PODMAN_NETWORK = 'containers.podman.podman_network'
PODMAN_CONTAINER = 'containers.podman.podman_container'
PODMAN_POD = 'containers.podman.podman_pod'

VOLUME_SAME = {}
NETWORK_SAME = {}
CONTAINER_SAME = {'ports', 'image', 'volumes'}

BUILD_CMD = 'podman build'  # 'buildah build' would also work

DEFAULT_REGISTRY = 'docker.io/library/'

# INPUT #


def read_yaml_from_file(infile):
    with open(infile, 'r') as fd:
        content = yaml.safe_load(fd)
    return content


# TRANSFORM #


def doco2podans(doco):
    tasks = []
    tasks += extract_networks(doco)
    tasks += extract_volumes(doco)
    tasks += extract_containers(doco)
    return tasks


def extract_networks(doco):
    networks = doco.get('networks')
    if not networks:
        return []
    network_tasks = []
    for name, value in networks.items():
        task = {
            'name': 'deploy network {}'.format(name),
            PODMAN_NETWORK: {'name': name}
        }
        # transfer options which are the same ones
        task[PODMAN_NETWORK].update(
            {x: y for x, y in value.items() if x in NETWORK_SAME})
        # FIXME handle for now remaining options to not forget them
        misc = {x: y for x, y in value.items() if x not in NETWORK_SAME}
        if misc:
            task[PODMAN_NETWORK]['misc'] = misc
        network_tasks.append(task)
    return network_tasks


def extract_volumes(doco):
    volumes = doco.get('volumes')
    if not volumes:
        return []
    volume_tasks = []
    for name, value in volumes.items():
        task = {
            'name': 'deploy volume {}'.format(name),
            PODMAN_VOLUME: {'name': name}
        }
        # transfer options which are the same ones
        task[PODMAN_VOLUME].update(
            {x: y for x, y in value.items() if x in VOLUME_SAME})
        # FIXME handle for now remaining options to not forget them
        misc = {x: y for x, y in value.items() if x not in VOLUME_SAME}
        if misc:
            task[PODMAN_VOLUME]['misc'] = misc
        volume_tasks.append(task)
    return volume_tasks


def extract_containers(doco):
    services = doco.get('services')
    if not services:
        return []
    container_tasks = []
    hashed_tasks = {}
    linked_containers = []
    for name, value in services.items():
        task = {
            'name': 'deploy container {}'.format(name),
            PODMAN_CONTAINER: {'name': name, 'hostname': name}
        }
        # transfer options which are the same ones
        task[PODMAN_CONTAINER].update(
            {x: y for x, y in value.items() if x in CONTAINER_SAME})
        # we take care of the remaining options
        misc = {x: y for x, y in value.items() if x not in CONTAINER_SAME}
        if 'build' in misc:
            build_task = {
                'name': 'build image for container {}'.format(name),
                'command': {
                    'cmd': BUILD_CMD + ' ' + misc['build']
                },
                'register': '__image_{}'.format(name)
            }
            container_tasks.append(build_task)
            task[PODMAN_CONTAINER]['image'] = \
                '{{{{ __image_{}.stdout_lines[-1] }}}}'.format(name)
            del misc['build']
        elif 'image' in task[PODMAN_CONTAINER]:
            if '/' not in task[PODMAN_CONTAINER]['image']:
                task[PODMAN_CONTAINER]['image'] = \
                    DEFAULT_REGISTRY + task[PODMAN_CONTAINER]['image']
        else:  # FIXME should be an error...
            sys.stderr.write(
                "WARNING: either 'build' or 'image' must be defined")
        # we keep together in networks containers which are somehow linked
        if 'links' in misc:
            found_networks = []
            for container in [name] + misc['links']:
                for network in linked_containers:
                    if container in network:
                        found_networks.append[network]
            if found_networks:
                for network in found_networks[1:]:
                    found_networks[0] |= network
                    linked_containers.delete[network]
                found_networks[0] |= set([name] + misc['links'])
            else:
                linked_containers.append(set([name] + misc['links']))
            del misc['links']
        # FIXME handle for now remaining options to not forget them
        if misc:
            task[PODMAN_CONTAINER]['misc'] = misc
        hashed_tasks[name] = task
        container_tasks.append(task)
    for network in linked_containers:
        network_name = "nw-" + "-".join(network)
        network_task = {
            'name': 'deploy network {}'.format(network_name),
            PODMAN_NETWORK: {
                'name': network_name,
            }
        }
        for container in network:
            # FIXME do we need to handle multiple networks?
            # FIXME would it be an alternative to use container:<name>
            hashed_tasks[container][PODMAN_CONTAINER]['network'] = network_name
        container_tasks.insert(0, network_task)
    return container_tasks


# OUTPUT #


def j2_filter_to_yaml(value, **params):
    return yaml.dump(value, Dumper=yaml.Dumper, **params)


def get_jinja2_environment(path='templates'):
    j2_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(path),
        autoescape=jinja2.select_autoescape()
    )
    j2_env.filters['to_yaml'] = j2_filter_to_yaml
    return j2_env


def generate_from_template(tasks, path='templates', kind='playbook'):
    j2_env = get_jinja2_environment(path)
    j2_template = j2_env.get_template('{kind}.yml.j2'.format(kind=kind))

    text = j2_template.render(tasks=tasks)

    return text


# MAIN #

if __name__ == '__main__':
    doco_struct = read_yaml_from_file(sys.argv[1])
    podans_tasks = doco2podans(doco_struct)
    podans_yaml = generate_from_template(
        tasks=podans_tasks,
        kind=sys.argv[2],
    )

    print(podans_yaml)
