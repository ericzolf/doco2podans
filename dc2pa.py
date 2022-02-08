#!/usr/bin/env python
import jinja2
import sys
import yaml

PODMAN_VOLUME = 'containers.podman.podman_volume'
PODMAN_NETWORK = 'containers.podman.podman_network'
PODMAN_CONTAINER = 'containers.podman.podman_container'

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
        task[PODMAN_NETWORK].update(value)
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
        task[PODMAN_VOLUME].update(value)
        volume_tasks.append(task)
    return volume_tasks


def extract_containers(doco):
    services = doco.get('services')
    if not services:
        return []
    container_tasks = []
    for name, value in services.items():
        task = {
            'name': 'deploy container {}'.format(name),
            PODMAN_CONTAINER: {'name': name}
        }
        task[PODMAN_CONTAINER].update(value)
        container_tasks.append(task)
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
