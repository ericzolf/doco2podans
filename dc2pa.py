#!/usr/bin/env python
import argparse
import collections
import graphlib
import jinja2
import re
import sys
import yaml

# names of podman ansible modules

ANSMOD = {
    'container': 'containers.podman.podman_container',
    'network': 'containers.podman.podman_network',
    'pod': 'containers.podman.podman_pod',
    'secret': 'containers.podman.podman_secret',
    'volume': 'containers.podman.podman_volume',
}

VOLUME_SAME = {}
NETWORK_SAME = {}
CONTAINER_SAME = {
    'ports': 'ports',
    'image': 'image',
    'command': 'command',
    'hostname': 'hostname',
    'volumes': 'volumes',
    'volumes_from': 'volumes_from',
    'restart': 'restart_policy',
    'secrets': 'secrets',
    'shm_size': 'shm_size',
}

BUILD_CMD = 'podman build'  # 'buildah build' would also work

DEFAULT_REGISTRY = 'docker.io'
DEFAULT_LIBRARY = 'library'

STATE_ACTION_MAP = {
    'present': 'deploy',
    'started': 'deploy',
    'absent': 'destroy',
}

ENV_REGEX = re.compile(r'\${?([A-Za-z_]+)}?')

# INPUT #


def read_doco_from_file(infile):
    """
    Read a Python structure from a Docker Compose file
    """
    content = yaml.safe_load(infile)
    return content


# TRANSFORM #


def doco2podans(doco, args):
    """
    Transforms a Docker Compose structure into a Podman Ansible one
    """
    tasks = []
    tasks += extract_secret_tasks(doco, args)
    tasks += extract_network_tasks(doco, args)
    tasks += extract_volume_tasks(doco, args)
    tasks += extract_container_tasks(doco, args)
    if args.state == 'absent':
        tasks.reverse()
    tasks = recurse_replace_envvars(tasks)
    return tasks


def extract_secret_tasks(doco, args):
    """
    Extract secret Ansible tasks from a Docker Compose structure
    """
    secrets = doco.get('secrets')
    if not secrets:
        return []
    tasks = []
    for name, value in secrets.items():
        task = get_stub_task(name, 'secret', args.state)
        if args.state == 'absent':
            tasks.append(task)
            continue
        task[ANSMOD['secret']]['data'] = \
            "{{{{ lookup('file', '{}') }}}}".format(value['file'])
        task[ANSMOD['secret']][args.secret_exists] = True
        tasks.append(task)

    return tasks


def extract_network_tasks(doco, args):
    """
    Extract network Ansible tasks from a Docker Compose structure
    """
    networks = doco.get('networks')
    if not networks:
        return []
    tasks = []
    for name, value in networks.items():
        task = get_stub_task(name, 'network', args.state)
        if args.state == 'absent':
            tasks.append(task)
            continue
        # transfer options which are the same ones
        same, rest = split_same_rest(value, NETWORK_SAME)
        task[ANSMOD['network']].update(same)
        if rest:
            sys.stderr.write(
                "WARNING: There are unsupported network options\n")
            task[ANSMOD['network']]['rest'] = rest
        tasks.append(task)
    return tasks


def extract_volume_tasks(doco, args):
    """
    Extract volume Ansible tasks from a Docker Compose structure
    """
    volumes = doco.get('volumes')
    if not volumes:
        return []
    tasks = []
    for name, value in volumes.items():
        task = get_stub_task(name, 'volume', args.state)
        if args.state == 'absent':
            tasks.append(task)
            continue
        # transfer options which are the same ones
        same, rest = split_same_rest(value, VOLUME_SAME)
        task[ANSMOD['volume']].update(same)
        if rest:
            sys.stderr.write(
                "WARNING: There are unsupported volume options\n")
            task[ANSMOD['volume']]['rest'] = rest
        tasks.append(task)
    return tasks


def extract_container_tasks(doco, args):
    """
    Extract volume Ansible tasks from a Docker Compose structure
    """
    services = doco.get('services')
    configs = doco.get('configs')
    if not services:
        return []
    tasks = []
    hashed_tasks = {}
    linked_containers = []
    shared_volume_containers = set()
    container_graph = collections.defaultdict(list)  # dependencies
    for name, value in services.items():
        task = get_stub_task(name, 'container', args.state)
        task_module = task[ANSMOD['container']]  # a kind of short link
        # transfer options which are the same ones
        same, rest = split_same_rest(value, CONTAINER_SAME)
        task_module.update(same)
        # we take care of the remaining options
        if 'build' in rest:
            if args.state == 'present':
                build_task = create_build_task(rest['build'], name,
                                               task_module)
                tasks.append(build_task)
            del rest['build']
        elif 'image' in task_module:
            improve_container_image(task_module)
        elif args.state == 'present':  # FIXME should be an error...
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
            task_module['env'] = extract_container_dict(rest['environment'])
            del rest['environment']
        if 'labels' in rest:
            task_module['labels'] = extract_container_dict(rest['labels'])
            del rest['labels']
        if 'depends_on' in rest:
            container_graph[name].extend(rest['depends_on'])
            if args.depends_network:
                extract_container_links([name] + rest['depends_on'],
                                        linked_containers)
            del rest['depends_on']
        if 'configs' in rest:
            add_configs_to_volumes(task_module, rest['configs'], configs)
            del rest['configs']
        # FIXME handle for now remaining options to not forget them
        if rest:
            sys.stderr.write(
                "WARNING: There are unsupported container options\n")
            task_module['rest'] = rest
        # save the created container task in a dict and in our tasks list
        hashed_tasks[name] = task
    # handle 
    for network in linked_containers:
        network_task = create_linked_network_task(network, hashed_tasks,
                                                  args.state)
        tasks.insert(0, network_task)
    # improve the volumes by adding SELinux labels
    for name, task in hashed_tasks.items():
        if 'volumes' in task[ANSMOD['container']]:
            improve_container_volume(name, task[ANSMOD['container']],
                                     shared_volume_containers)

    # Finally add the container tasks according to dependencies
    if container_graph:
        # we want to keep as much as possible the initial order hence we
        # first add the containers without any dependencies
        for task_name in hashed_tasks:
            if task_name not in container_graph:
                tasks.append(hashed_tasks[task_name])
        # then we sort and add the containers with dependencies
        topo_sorter = graphlib.TopologicalSorter(container_graph)
        sorted_containers = tuple(topo_sorter.static_order())
        for task_name in sorted_containers:
            if task_name in container_graph:
                tasks.append(hashed_tasks[task_name])
    else:
        for task_name in hashed_tasks:
            tasks.append(hashed_tasks[task_name])

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
    count = task_module['image'].count('/')
    if count == 0:
        task_module['image'] = '/'.join(
            (DEFAULT_REGISTRY, DEFAULT_LIBRARY, task_module['image']))
    elif count == 1:
        task_module['image'] = '/'.join(
            (DEFAULT_REGISTRY, task_module['image']))


def extract_container_dict(settings):
    """
    Return a potential list of x=y settings into a dictionary,
    or the dictionary itself
    """
    if isinstance(settings, dict):
        return settings
    else:
        return dict([x.split('=', maxsplit=1) for x in settings])


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


def create_linked_network_task(network, hashed_tasks, state):
    """
    Create a network task and link it to the container tasks linked together.

    Return the network task.
    """
    network_name = "nw-" + "-".join(network)
    network_task = get_stub_task(network_name, 'network', state)
    for container in network:
        # FIXME do we need to handle multiple networks?
        # FIXME would it be an alternative to use container:<name>
        hashed_tasks[container][ANSMOD['container']]['network'] = network_name
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


def get_stub_task(name, element, state):
    """
    Return a task with task name, Ansible podman module, element name and state
    """
    task_name = '{} {} {}'.format(STATE_ACTION_MAP[state], element, name)
    task = {
        'name': task_name,
        ANSMOD[element]: {
            'name': name,
        }
    }
    # the 'present' state is the default state
    if state != 'present':
        task[ANSMOD[element]]['state'] = state
    return task


def add_configs_to_volumes(task_module, task_configs, configs):
    if 'volumes' not in task_module:
        task_module['volumes'] = []
    for config in task_configs:
        if isinstance(config, dict):
            source = configs[config['source']]['file']
            target = config.get('target', '/' + config['source'])
        else:
            source = configs[config]['file']
            target = '/' + config
        # configs are always potentially shared, hence (small) z
        task_module['volumes'].append(':'.join((source, target, 'z')))


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
                        help='kind of Ansible file to create')
    parser.add_argument('--state', default='present',
                        choices=['present', 'absent'],
                        help='deploy or destroy')
    parser.add_argument('--secret-exists', default='skip_existing',
                        choices=['skip_existing', 'force'],
                        help='how to handle existing secrets, '
                        'leave as is or force replacement')
    parser.add_argument('--depends-network',
                        action=argparse.BooleanOptionalAction,
                        help='create network out of dependencies')
    parser.add_argument('doco', type=argparse.FileType('r'),
                        help='a source docker compose file')
    parser.add_argument('podans', type=argparse.FileType('w'),
                        default=sys.stdout, nargs='?',
                        help='a target Ansible file')
    parsed_args = parser.parse_args()
    return parsed_args


def recurse_replace_envvars(struct):
    """
    Replace environment variables of the form $XXX or ${ENV} recursively

    Returns the structure with replaced environment variables
    """
    if isinstance(struct, list):
        return [recurse_replace_envvars(x) for x in struct]
    elif isinstance(struct, dict):
        return {x: recurse_replace_envvars(y) for x,y in struct.items()}
    elif isinstance(struct, str):
        return ENV_REGEX.sub(r"{{ lookup('env', '\1') }}", struct)
    else:
        return struct


# MAIN #

if __name__ == '__main__':
    args = parse_arguments()
    doco_struct = read_doco_from_file(args.doco)
    podans_struct = doco2podans(doco_struct, args)
    podans_yaml = generate_from_template(
        tasks=podans_struct,
        kind=args.kind,
    )
    args.podans.write(podans_yaml)
