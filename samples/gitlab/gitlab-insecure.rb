external_url 'http://localhost.localdomain:8888/'
gitlab_rails['initial_root_password'] = File.read('/run/secrets/gitlab_root_password')
gitlab_rails['gitlab_shell_ssh_port'] = 2222
