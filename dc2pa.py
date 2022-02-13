#!/usr/bin/env python
import argparse
import collections
import graphlib
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


def split_same_rest(dictionary, same_map):
    """
    Split a dictionary between keys in the same_map and keys which aren't.

    Returns both same (with mapped keys) and rest dictionaries.
    """
    if dictionary is None:
        return {}, None
    same = {same_map[x]: y for x, y in dictionary.items() if x in same_map}
    rest = {x: y for x, y in dictionary.items() if x not in same_map}
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
    container_graph = collections.defaultdict(list)  # dependencies
    for name, value in services.items():
        task = {
            'name': 'deploy container {}'.format(name),
            PODMAN_CONTAINER: {'name': name, 'hostname': name}
        }
        task_module = task[PODMAN_CONTAINER]  # a kind of short link
        # transfer options which are the same ones
        same, rest = split_same_rest(value, CONTAINER_SAME)
        task_module.update(same)
        # we take care of the remaining options
        if 'build' in rest:
            build_task = create_build_task(rest['build'], name,
                                           task_module)
            container_tasks.append(build_task)
            del rest['build']
        elif 'image' in task_module:
            improve_container_image(task_module)
        else:  # FIXME should be an error...
            sys.stderr.write(
                "WARNING: either 'build' or 'image' must be defined")
        # keep trace of linked volumes
        if 'volumes_from' in task_module:
            shared_volume_containers |= set(task_module['volumes_from'])
            container_graph[name].extend(task_module['volumes_from'])
        # we keep together in networks containers which are somehow linked
        if 'links' in rest:
            extract_container_links([name] + rest['links'], linked_containers)
            del rest['links']
        if 'environment' in rest:
            copy_container_env(rest['environment'], task_module)
            del rest['environment']
        if 'depends_on' in rest:
            container_graph[name].extend(rest['depends_on'])
            del rest['depends_on']
        # FIXME handle for now remaining options to not forget them
        if rest:
            sys.stderr.write(
                "WARNING: There are unsupported container options\n")
            task_module['rest'] = rest
        # save the created container task in a dict and in our tasks list
        hashed_tasks[name] = task
    # handle 
    for network in linked_containers:
        network_task = create_linked_network_task(network, hashed_tasks)
        container_tasks.insert(0, network_task)
    # improve the volumes by adding SELinux labels
    for name, task in hashed_tasks.items():
        if 'volumes' in task_module:
            improve_container_volume(name, task_module,
                                     shared_volume_containers)

    # Finally add the container tasks according to dependencies
    if container_graph:
        # we want to keep as much as possible the initial order hence we
        # first add the containers without any dependencies
        for task_name in hashed_tasks:
            if task_name not in container_graph:
                container_tasks.append(hashed_tasks[task_name])
        # then we sort and add the containers with dependencies
        topo_sorter = graphlib.TopologicalSorter(container_graph)
        sorted_containers = tuple(topo_sorter.static_order())
        for task_name in sorted_containers:
            if task_name in container_graph:
                container_tasks.append(hashed_tasks[task_name])
    else:
        for task_name in hashed_tasks:
            container_tasks.append(hashed_tasks[task_name])

    return container_tasks


def create_build_task(build, container_name, task_module):
    """
    Create a container image build task and link it to the container task.

    Return the build task.
    """
    build_task = {
        'name': 'build image for container {}'.format(container_name),
        'command': {
            'cmd': BUILD_CMD + ' ' + rest['build']
        },
        'register': '__image_{}'.format(container_name)
    }
    task_module['image'] = '{{{{ __image_{}.stdout_lines[-1] }}}}'.format(
        container_name)
    return build_task


def improve_container_image(task_module):
    """
    Prefix plain image name with a default registry path
    """
    if '/' not in task_module['image']:
        task_module['image'] = DEFAULT_REGISTRY + task_module['image']


def copy_container_env(environment, task_module):
    """
    Copy the environment into the 'env' option of the container task
    """
    task_module['env'] = dict([x.split('=', maxsplit=1) for x in environment])


def extract_container_links(links, linked_containers):
    """
    Extract the containers belonging into the same network and save them into
    the linked_containers variable

    Two different networks containing the same container need to be merged
    """
    found_networks = []
    for container in links:
        for network in linked_containers:
            if container in network:
                found_networks.append(network)
    if found_networks:
        for network in found_networks[1:]:
            found_networks[0] |= network
            linked_containers.delete[network]
        found_networks[0] |= set(links)
    else:
        linked_containers.append(set(links))


def create_linked_network_task(network, hashed_tasks):
    """
    Create a network task and link it to the container tasks linked together.

    Return the network task.
    """
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
    return network_task


def improve_container_volume(name, task_module, shared_volume_containers):
    """
    Add where necessary a shared or individual SELinux label to volumes
    """
    if name in shared_volume_containers:
        label = 'z'  # shared SELinux label
    else:
        label = 'Z'  # individual SELinux label
    for idx, vol in enumerate(task_module['volumes']):
        vols = vol.split(':')
        if len(vols) < 3:
            task_module['volumes'][idx] = ":".join(vols + [label])
        else:
            opts = vols[-1].split(',')
            if 'z' not in opts and 'Z' not in opts:
                task_module['volumes'][idx] = ":".join(
                    vols[:-1] + [','.join(opts + [label])])


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
