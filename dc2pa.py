#!/usr/bin/env python
import jinja2
import yaml


def j2_filter_to_yaml(value):
    return yaml.dump(value, Dumper=yaml.Dumper)


def get_jinja2_environment(path='templates'):
    j2_env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(path),
        autoescape=jinja2.select_autoescape()
    )
    j2_env.filters['to_yaml'] = j2_filter_to_yaml
    return j2_env


j2_env = get_jinja2_environment()
j2_template = j2_env.get_template('{kind}.yml.j2'.format(kind='tasks'))

text = j2_template.render(tasks=[{'a': 1, 'b': 2}, {'x': 3, 'y': 4}])

print(text)
