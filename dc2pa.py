#!/usr/bin/env python
import jinja2

j2_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader('templates'),
    autoescape=jinja2.select_autoescape()
)

j2_template = j2_env.get_template('{kind}.yml.j2'.format(kind='playbook'))

text = j2_template.render(tasks=[{'a': 1, 'b': 2}, {'x': 3, 'y': 4}])

print(text)
