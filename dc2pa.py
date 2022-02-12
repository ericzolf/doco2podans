#!/usr/bin/env python
import argparse
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
CONTAINER_SAME = {
    'ports': 'ports',
    'image': 'image',
    'command': 'command',
    'volumes': 'volumes',
    'volumes_from': 'volumes_from',
    'restart': 'restart_policy',
}

BUILD_CMD = 'podman build'  # 'buildah build' would also work

DEFAULT_REGISTRY = 'docker.io/library/'

# INPUT #


def read_doco_from_file(infile):
    """
    Read a Python structure from a Docker Compose file
    """
    content = yaml.safe_load(infile)
    return content


# TRANSFORM #


def doco2podans(doco):
    """
    Transforms a Docker Compose structure into a Podman Ansible one
    """
    tasks = []
    tasks += extract_network_tasks(doco)
    tasks += extract_volume_tasks(doco)
    tasks += extract_container_tasks(doco)
    return tasks


def split_same_rest(value, same_map):
    if value is None:
        return {}, None
    same = {same_map[x]: y for x, y in value.items() if x in same_map}
    rest = {x: y for x, y in value.items() if x not in same_map}
    return same, rest


def extract_network_tasks(doco):
    """
    Extract network Ansible tasks from a Docker Compose structure
    """
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
        same, rest = split_same_rest(value, NETWORK_SAME)
        task[PODMAN_NETWORK].update(same)
        if rest:
            sys.stderr.write(
                "WARNING: There are unsupported network options\n")
            task[PODMAN_NETWORK]['rest'] = rest
        network_tasks.append(task)
    return network_tasks


def extract_volume_tasks(doco):
    """
    Extract volume Ansible tasks from a Docker Compose structure
    """
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
        same, rest = split_same_rest(value, VOLUME_SAME)
        task[PODMAN_VOLUME].update(same)
        if rest:
            sys.stderr.write(
                "WARNING: There are unsupported volume options\n")
            task[PODMAN_VOLUME]['rest'] = rest
        volume_tasks.append(task)
    return volume_tasks


def extract_container_tasks(doco):
    """
    Extract volume Ansible tasks from a Docker Compose structure
    """
    services = doco.get('services')
    if not services:
        return []
    container_tasks = []
    hashed_tasks = {}
    linked_containers = []
    shared_volume_containers = set()
    for name, value in services.items():
        task = {
            'name': 'deploy container {}'.format(name),
            PODMAN_CONTAINER: {'name': name, 'hostname': name}
        }
        # transfer options which are the same ones
        same, rest = split_same_rest(value, CONTAINER_SAME)
        task[PODMAN_CONTAINER].update(same)
        # we take care of the remaining options
        if 'build' in rest:
            build_task = {
                'name': 'build image for container {}'.format(name),
                'command': {
                    'cmd': BUILD_CMD + ' ' + rest['build']
                },
                'register': '__image_{}'.format(name)
            }
            container_tasks.append(build_task)
            task[PODMAN_CONTAINER]['image'] = \
                '{{{{ __image_{}.stdout_lines[-1] }}}}'.format(name)
            del rest['build']
        elif 'image' in task[PODMAN_CONTAINER]:
            if '/' not in task[PODMAN_CONTAINER]['image']:
                task[PODMAN_CONTAINER]['image'] = \
                    DEFAULT_REGISTRY + task[PODMAN_CONTAINER]['image']
        else:  # FIXME should be an error...
            sys.stderr.write(
                "WARNING: either 'build' or 'image' must be defined")
        # keep trace of linked volumes
        if 'volumes_from' in task[PODMAN_CONTAINER]:
            shared_volume_containers |= set(
                task[PODMAN_CONTAINER]['volumes_from'])
        # we keep together in networks containers which are somehow linked
        if 'links' in rest:
            found_networks = []
            for container in [name] + rest['links']:
                for network in linked_containers:
                    if container in network:
                        found_networks.append(network)
            if found_networks:
                for network in found_networks[1:]:
                    found_networks[0] |= network
                    linked_containers.delete[network]
                found_networks[0] |= set([name] + rest['links'])
            else:
                linked_containers.append(set([name] + rest['links']))
            del rest['links']
        if 'environment' in rest:
            task[PODMAN_CONTAINER]['env'] = dict(
                [x.split('=', maxsplit=1) for x in rest['environment']])
            del rest['environment']
        # FIXME handle for now remaining options to not forget them
        if rest:
            sys.stderr.write(
                "WARNING: There are unsupported container options\n")
            task[PODMAN_CONTAINER]['rest'] = rest
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
    # we improve the volumes by adding SELinux labels
    for name, task in hashed_tasks.items():
        if 'volumes' in task[PODMAN_CONTAINER]:
            if name in shared_volume_containers:
                label = 'z'  # shared SELinux label
            else:
                label = 'Z'  # individual SELinux label
            for idx, vol in enumerate(task[PODMAN_CONTAINER]['volumes']):
                vols = vol.split(':')
                if len(vols) < 3:
                    task[PODMAN_CONTAINER]['volumes'][idx] = ":".join(
                        vols + [label])
                else:
                    opts = vols[-1].split(',')
                    if 'z' not in opts and 'Z' not in opts:
                        task[PODMAN_CONTAINER]['volumes'][idx] = ":".join(
                            vols[:-1] + [','.join(opts + [label])])

    return container_tasks


# OUTPUT #


def j2_filter_to_yaml(value, **params):
    """
    Implements a to_yaml filter for Jinja2 templates
    """
    return yaml.dump(value, Dumper=yaml.Dumper, **params)


def get_jinja2_environment(path='templates'):
    """
    Get a Jinja2 environment from the templates directory
    """
    j2_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(path),
        autoescape=jinja2.select_autoescape()
    )
    j2_env.filters['to_yaml'] = j2_filter_to_yaml
    return j2_env


def generate_from_template(tasks, path='templates', kind='playbook'):
    """
    Generate a string from a certain structure, assumed to be Ansible tasks

    path is the directory where to find the template of the kind given
    """
    j2_env = get_jinja2_environment(path)
    j2_template = j2_env.get_template('{kind}.yml.j2'.format(kind=kind))

    text = j2_template.render(tasks=tasks)

    return text


def parse_arguments():
    """
    Parse arguments from sys.argv

    Returns the parsed arguments
    """
    parser = argparse.ArgumentParser(
        description="Translate Docker Compose to Podman Ansible")
    parser.add_argument('--kind', default='playbook',
                        choices=['playbook', 'tasks'],
                        help='sum the integers (default: find the max)')
    parser.add_argument('doco', type=argparse.FileType('r'),
                        help='a source docker compose file')
    parser.add_argument('podans', type=argparse.FileType('w'),
                        default=sys.stdout, nargs='?',
                        help='a target Ansible file')
    parsed_args = parser.parse_args()
    return parsed_args


# MAIN #

if __name__ == '__main__':
    args = parse_arguments()
    doco_struct = read_doco_from_file(args.doco)
    podans_struct = doco2podans(doco_struct)
    podans_yaml = generate_from_template(
        tasks=podans_struct,
        kind=args.kind,
    )
    args.podans.write(podans_yaml)
