#!/usr/bin/env python
import jinja2
import sys
import yaml


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
    networks = doco.get('networks', [])
    if not networks:
        return []
    result = [{x: y} for x, y in networks.items()]
    return result


def extract_volumes(doco):
    volumes = doco.get('volumes', [])
    if not volumes:
        return []
    result = [{x: y} for x, y in volumes.items()]
    return result


def extract_containers(doco):
    services = doco.get('services', [])
    if not services:
        return []
    result = [{x: y} for x, y in services.items()]
    return result


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
    doco_yaml = read_yaml_from_file(sys.argv[1])
    podans_tasks = doco2podans(doco_yaml)
    podans_yaml = generate_from_template(
        tasks=podans_tasks,
        kind=sys.argv[2],
    )

    print(podans_yaml)
