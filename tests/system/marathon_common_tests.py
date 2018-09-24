"""This module contains tests which are supposed to run on both root Marathon and Marathon on Marathon (MoM)."""

import apps
import common
import groups
import os
import os.path
import pytest
import retrying
import scripts
import shakedown
import time

from shakedown import http, dcos_version_less_than, marthon_version_less_than, required_private_agents # NOQA
from shakedown.clients import marathon
from shakedown.errors import DCOSException
from matcher import assert_that, eventually, has_len, has_value, has_values, prop
from precisely import contains_string, equal_to, not_
from utils import get_cluster_local_domain, count_agents_in_faultdomains, get_biggest_cluster_region, get_app_domains, \
    get_used_regions_and_zones, get_all_cluster_regions


def test_launch_mesos_container():
    """Launches a Mesos container with a simple command."""

    app_def = apps.mesos_app(app_id='/mesos-container-app')
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    app = client.get_app(app_id)

    assert len(tasks) == 1, "The number of tasks is {} after deployment, but only 1 was expected".format(len(tasks))
    assert app['container']['type'] == 'MESOS', "The container type is not MESOS"


def test_launch_docker_container():
    """Launches a Docker container on Marathon."""

    app_def = apps.docker_http_server(app_id='/launch-docker-container-app')
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    app = client.get_app(app_id)

    assert len(tasks) == 1, "The number of tasks is {} after deployment, but only 1 was expected".format(len(tasks))
    assert app['container']['type'] == 'DOCKER', "The container type is not DOCKER"


def test_launch_mesos_container_with_docker_image():
    """Launches a Mesos container with a Docker image."""

    app_def = apps.ucr_docker_http_server(app_id='/launch-mesos-container-with-docker-image-app')
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    assert_that(lambda: client.get_tasks(app_id),
                eventually(has_len(equal_to(1)), max_attempts=30))

    app = client.get_app(app_id)
    assert app['container']['type'] == 'MESOS', "The container type is not MESOS"


# This fails on DC/OS 1.7, it is likely the version of Marathon in Universe for 1.7, is 1.1.5.
@shakedown.dcos_1_8
def test_launch_mesos_grace_period(marathon_service_name):
    """Tests 'taskKillGracePeriodSeconds' option using a Mesos container in a Marathon environment.
       Read more details about this test in `test_root_marathon.py::test_launch_mesos_root_marathon_grace_period`
    """

    app_id = '/mesos-grace-period-app'
    app_def = apps.mesos_app(app_id)

    default_grace_period = 3
    grace_period = 20

    app_def['fetch'] = [{"uri": "https://downloads.mesosphere.com/testing/test.py"}]
    app_def['cmd'] = '/opt/mesosphere/bin/python test.py'
    app_def['taskKillGracePeriodSeconds'] = grace_period
    task_name = app_id.lstrip('/')

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = shakedown.get_service_task(marathon_service_name, task_name)
    assert tasks is not None

    client.scale_app(app_id, 0)
    tasks = shakedown.get_service_task(marathon_service_name, task_name)
    assert tasks is not None

    # tasks should still be here after the default_grace_period
    time.sleep(default_grace_period + 1)
    tasks = shakedown.get_service_task(marathon_service_name, task_name)
    assert tasks is not None

    # but not after the set grace_period
    time.sleep(grace_period)
    tasks = shakedown.get_service_task(marathon_service_name, task_name)
    assert tasks is None


def test_launch_docker_grace_period(marathon_service_name):
    """Tests 'taskKillGracePeriodSeconds' option using a Docker container in a Marathon environment.
       Read more details about this test in `test_root_marathon.py::test_launch_mesos_root_marathon_grace_period`
    """

    app_id = '/launch-docker-grace-period-app'
    app_def = apps.docker_http_server(app_id)
    app_def['container']['docker']['image'] = 'kensipe/python-test'

    default_grace_period = 3
    grace_period = 20
    app_def['taskKillGracePeriodSeconds'] = grace_period
    app_def['cmd'] = 'python test.py'
    task_name = app_id.lstrip('/')

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = shakedown.get_service_task(marathon_service_name, task_name)
    assert tasks is not None

    client.scale_app(app_id, 0)
    tasks = shakedown.get_service_task(marathon_service_name, task_name)
    assert tasks is not None

    # tasks should still be here after the default_graceperiod
    time.sleep(default_grace_period + 1)
    tasks = shakedown.get_service_task(marathon_service_name, task_name)
    assert tasks is not None

    # but not after the set grace_period
    time.sleep(grace_period)
    assert_that(lambda: shakedown.get_service_task(marathon_service_name, task_name),
                eventually(equal_to(None), max_attempts=30))


def test_docker_port_mappings():
    """Tests that Docker ports are mapped and are accessible from the host."""

    app_def = apps.docker_http_server(app_id='/docker-port-mapping-app')
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    host = tasks[0]['host']
    port = tasks[0]['ports'][0]
    cmd = r'curl -s -w "%{http_code}"'
    cmd = cmd + ' {}:{}/.dockerenv'.format(host, port)
    status, output = shakedown.run_command_on_agent(host, cmd)

    assert status and output == "200", "HTTP status code is {}, but 200 was expected".format(output)


def test_docker_dns_mapping(marathon_service_name):
    """Tests that a running Docker task is accessible via DNS."""

    app_def = apps.docker_http_server(app_id='/docker-dns-mapping-app')
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    bad_cmd = 'ping -c 1 docker-test.marathon-user.mesos-bad'
    status, output = shakedown.run_command_on_master(bad_cmd)
    assert not status

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_dns():
        dnsname = '{}.{}.mesos'.format(app_id.lstrip('/'), marathon_service_name)
        cmd = 'ping -c 1 {}'.format(dnsname)
        shakedown.wait_for_dns(dnsname)
        status, output = shakedown.run_command_on_master(cmd)
        assert status, "ping failed for app using DNS lookup: {}".format(dnsname)

    check_dns()


def test_launch_app_timed():
    """Most tests wait until a task is launched with no reference to time.
       This test verifies that if a app is launched on marathon that within 3 secs there is a task spawned.
    """

    app_def = apps.mesos_app(app_id='/timed-launch-app')

    client = marathon.create_client()
    client.add_app(app_def)

    # if not launched in 3 sec fail
    time.sleep(3)
    tasks = client.get_tasks(app_def["id"])

    assert len(tasks) == 1, "The number of tasks is {} after deployment, but 1 was expected".format(len(tasks))


def test_ui_available(marathon_service_name):
    """Simply verifies that a request to the UI endpoint is successful if Marathon is launched."""

    response = http.get("{}/ui/".format(shakedown.dcos_service_url(marathon_service_name)))
    assert response.status_code == 200, "HTTP status code is {}, but 200 was expected".format(response.status_code)


def test_task_failure_recovers():
    """Tests that if a task is KILLED, another one will be launched with a different ID."""

    app_def = apps.sleep_app()
    app_def['cmd'] = 'sleep 1000'
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    old_task_id = tasks[0]['id']
    host = tasks[0]['host']

    common.kill_process_on_host(host, '[s]leep 1000')

    assert_that(lambda: client.get_tasks(app_id)[0],
                eventually(has_value('id', not_(equal_to(old_task_id))), max_attempts=30))


@pytest.mark.skipif("shakedown.ee_version() == 'strict'")
def test_run_app_with_specified_user():
    """Runs an app with a given user (cnetos). CentOS is expected, since it has centos user by default."""

    app_def = apps.sleep_app()
    app_def['user'] = 'centos'
    app_id = app_def['id']

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    task = tasks[0]
    assert task['state'] == 'TASK_RUNNING', "The task is not running: {}".format(task['state'])

    app = client.get_app(app_id)
    assert app['user'] == 'centos', "The app's user is not centos: {}".format(app['user'])


@pytest.mark.skipif("shakedown.ee_version() == 'strict'")
def test_run_app_with_non_existing_user():
    """Runs an app with a non-existing user, which should be failing."""

    app_def = apps.sleep_app()
    app_def['user'] = 'bad'

    client = marathon.create_client()
    client.add_app(app_def)

    assert_that(lambda: client.get_app(app_def["id"]), eventually(
        prop(['lastTaskFailure', 'message'], contains_string("No such user 'bad'")), max_attempts=30))


def test_run_app_with_non_downloadable_artifact():
    """Runs an app with a non-downloadable artifact."""

    app_def = apps.sleep_app()
    app_def['fetch'] = [{"uri": "http://localhost/missing-artifact"}]

    client = marathon.create_client()
    client.add_app(app_def)

    assert_that(lambda: client.get_app(app_def["id"]), eventually(
        prop(['lastTaskFailure', 'message'], contains_string("Failed to fetch all URIs for container")), max_attempts=30)) # NOQA E501


def test_launch_group():
    """Launches a group of 2 apps."""

    group_def = groups.sleep_group()
    groups_id = group_def["groups"][0]["id"]
    app_id = group_def["groups"][0]["apps"][0]["id"]

    client = marathon.create_client()
    client.create_group(group_def)

    common.deployment_wait(service_id=app_id)

    group_apps = client.get_group(groups_id)
    apps = group_apps['apps']
    assert len(apps) == 2, "The numbers of apps is {} after deployment, but 2 is expected".format(len(apps))


@shakedown.private_agents(2)
def test_launch_and_scale_group():
    """Launches and scales a group."""

    group_def = groups.sleep_group()
    groups_id = group_def["groups"][0]["id"]
    app1_id = group_def["groups"][0]["apps"][0]["id"]
    app2_id = group_def["groups"][0]["apps"][1]["id"]

    client = marathon.create_client()
    client.create_group(group_def)

    common.deployment_wait(service_id=app1_id)

    group_apps = client.get_group(groups_id)
    apps = group_apps['apps']
    assert len(apps) == 2, "The number of apps is {}, but 2 was expected".format(len(apps))

    tasks1 = client.get_tasks(app1_id)
    tasks2 = client.get_tasks(app2_id)
    assert len(tasks1) == 1, "The number of tasks #1 is {} after deployment, but 1 was expected".format(len(tasks1))
    assert len(tasks2) == 1, "The number of tasks #2 is {} after deployment, but 1 was expected".format(len(tasks2))

    # scale by 2 for the entire group
    client.scale_group(groups_id, 2)
    common.deployment_wait(service_id=app1_id)

    tasks1 = client.get_tasks(app1_id)
    tasks2 = client.get_tasks(app2_id)
    assert len(tasks1) == 2, "The number of tasks #1 is {} after scale, but 2 was expected".format(len(tasks1))
    assert len(tasks2) == 2, "The number of tasks #2 is {} after scale, but 2 was expected".format(len(tasks2))


@shakedown.private_agents(2)
def test_scale_app_in_group():
    """Scales an individual app in a group."""

    group_def = groups.sleep_group()
    groups_id = group_def["groups"][0]["id"]
    app1_id = group_def["groups"][0]["apps"][0]["id"]
    app2_id = group_def["groups"][0]["apps"][1]["id"]

    client = marathon.create_client()
    client.create_group(group_def)

    common.deployment_wait(service_id=app1_id)

    group_apps = client.get_group(groups_id)
    apps = group_apps['apps']
    assert len(apps) == 2, "The number of apps is {}, but 2 was expected".format(len(apps))

    tasks1 = client.get_tasks(app1_id)
    tasks2 = client.get_tasks(app2_id)
    assert len(tasks1) == 1, "The number of tasks #1 is {} after deployment, but 1 was expected".format(len(tasks1))
    assert len(tasks2) == 1, "The number of tasks #2 is {} after deployment, but 1 was expected".format(len(tasks2))

    # scaling just one app in the group
    client.scale_app(app1_id, 2)
    common.deployment_wait(service_id=app1_id)

    tasks1 = client.get_tasks(app1_id)
    tasks2 = client.get_tasks(app2_id)
    assert len(tasks1) == 2, "The number of tasks #1 is {} after scale, but 2 was expected".format(len(tasks1))
    assert len(tasks2) == 1, "The number of tasks #2 is {} after scale, but 1 was expected".format(len(tasks2))


@shakedown.private_agents(2)
def test_scale_app_in_group_then_group():
    """First scales an app in a group, then scales the group itself."""

    group_def = groups.sleep_group()
    groups_id = group_def["groups"][0]["id"]
    app1_id = group_def["groups"][0]["apps"][0]["id"]
    app2_id = group_def["groups"][0]["apps"][1]["id"]

    client = marathon.create_client()
    client.create_group(group_def)

    common.deployment_wait(service_id=app1_id)

    group_apps = client.get_group(groups_id)
    apps = group_apps['apps']
    assert len(apps) == 2, "The number of apps is {}, but 2 was expected".format(len(apps))

    tasks1 = client.get_tasks(app1_id)
    tasks2 = client.get_tasks(app2_id)
    assert len(tasks1) == 1, "The number of tasks #1 is {} after deployment, but 1 was expected".format(len(tasks1))
    assert len(tasks2) == 1, "The number of tasks #2 is {} after deployment, but 1 was expected".format(len(tasks2))

    # scaling just one app in the group
    client.scale_app(app1_id, 2)
    common.deployment_wait(service_id=app1_id)

    tasks1 = client.get_tasks(app1_id)
    tasks2 = client.get_tasks(app2_id)
    assert len(tasks1) == 2, "The number of tasks #1 is {} after scale, but 2 was expected".format(len(tasks1))
    assert len(tasks2) == 1, "The number of tasks #2 is {} after scale, but 1 was expected".format(len(tasks2))
    common.deployment_wait(service_id=app1_id)

    # scaling the group after one app in the group was scaled
    client.scale_group(groups_id, 2)
    common.deployment_wait(service_id=app1_id)

    tasks1 = client.get_tasks(app1_id)
    tasks2 = client.get_tasks(app2_id)
    assert len(tasks1) == 4, "The number of tasks #1 is {} after scale, but 4 was expected".format(len(tasks1))
    assert len(tasks2) == 2, "The number of tasks #2 is {} after scale, but 2 was expected".format(len(tasks2))


def assert_app_healthy(client, app_def, health_check):
    app_def['healthChecks'] = [health_check]
    instances = app_def['instances']
    app_id = app_def["id"]

    print('Testing {} health check protocol.'.format(health_check['protocol']))
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id, max_attempts=300)

    app = client.get_app(app_id)
    assert app['tasksRunning'] == instances, \
        "The number of running tasks is {}, but {} was expected".format(app['tasksRunning'], instances)
    assert app['tasksHealthy'] == instances, \
        "The number of healthy tasks is {}, but {} was expected".format(app['tasksHealthy'], instances)


@shakedown.dcos_1_9
@pytest.mark.parametrize('protocol', ['HTTP', 'MESOS_HTTP', 'TCP', 'MESOS_TCP'])
def test_http_health_check_healthy(protocol):
    """Tests HTTP, MESOS_HTTP, TCP and MESOS_TCP health checks against a web-server in Python."""

    app_def = apps.http_server()
    client = marathon.create_client()
    assert_app_healthy(client, app_def, common.health_check(protocol=protocol))


def test_app_with_no_health_check_not_healthy():
    """Makes sure that no task is marked as healthy if no health check is defined for the corresponding app."""

    app_def = apps.sleep_app()
    app_id = app_def["id"]
    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    app = client.get_app(app_id)

    assert app['tasksRunning'] == 1, \
        "The number of running tasks is {}, but 1 was expected".format(app['tasksRunning'])
    assert app['tasksHealthy'] == 0, \
        "The number of healthy tasks is {}, but 0 was expected".format(app['tasksHealthy'])


def test_command_health_check_healthy():
    """Tests COMMAND health check"""

    app_def = apps.sleep_app()
    client = marathon.create_client()
    assert_app_healthy(client, app_def, common.command_health_check())


@shakedown.dcos_1_9
@pytest.mark.parametrize('protocol', ['HTTPS', 'MESOS_HTTPS'])
def test_https_health_check_healthy(protocol):
    """Tests HTTPS and MESOS_HTTPS health checks using a prepared nginx image that enables
       SSL (using self-signed certificate) and listens on 443.
    """
    # marathon version captured here will work for root and mom
    requires_marathon_version('1.4.2')

    client = marathon.create_client()
    app_def = apps.docker_nginx_ssl()
    assert_app_healthy(client, app_def, common.health_check(protocol=protocol, port_index=1))


def test_failing_health_check_results_in_unhealthy_app():
    """Tests failed health checks of an app. The health check is meant to never pass."""

    app_def = apps.http_server()
    app_def['healthChecks'] = [common.health_check('/bad-url', 'HTTP', failures=0, timeout=3)]

    client = marathon.create_client()
    client.add_app(app_def)

    assert_that(lambda: client.get_app(app_def["id"]), eventually(
        has_values(tasksRunning=1, tasksHealthy=0, tasksUnhealthy=1), max_attempts=30))


@shakedown.private_agents(2)
def test_task_gets_restarted_due_to_network_split():
    """Verifies that a health check fails in presence of a network partition."""

    app_def = apps.http_server()
    app_id = app_def["id"]
    app_def['healthChecks'] = [common.health_check()]
    common.pin_to_host(app_def, common.ip_other_than_mom())

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    app = client.get_app(app_id)
    assert app['tasksRunning'] == 1, \
        "The number of running tasks is {}, but 1 was expected".format(app['tasksRunning'])
    assert app['tasksHealthy'] == 1, \
        "The number of healthy tasks is {}, but 1 was expected".format(app['tasksHealthy'])

    tasks = client.get_tasks(app_id)
    task_id = tasks[0]['id']
    host = tasks[0]['host']
    port = tasks[0]['ports'][0]

    # introduce a network partition
    common.block_iptable_rules_for_seconds(host, port, sleep_seconds=10, block_input=True, block_output=False)

    common.deployment_wait(service_id=app_id)

    app = client.get_app(app_id)
    tasks = client.get_tasks(app_id)
    new_task_id = tasks[0]['id']
    assert task_id != new_task_id, "The task didn't get killed because of a failed health check"

    assert app['tasksRunning'] == 1, \
        "The number of running tasks is {}, but 1 was expected".format(app['tasksRunning'])
    assert app['tasksHealthy'] == 1, \
        "The number of healthy tasks is {}, but 0 was expected".format(app['tasksHealthy'])

    # network partition should cause a task restart
    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_health_message():
        tasks = client.get_tasks(app_id)
        new_task_id = tasks[0]['id']
        assert task_id != new_task_id, "The task has not been restarted: {}".format(task_id)

        app = client.get_app(app_id)
        assert app['tasksRunning'] == 1, \
            "The number of running tasks is {}, but 1 was expected".format(app['tasksRunning'])
        assert app['tasksHealthy'] == 1, \
            "The number of healthy tasks is {}, but 1 was expected".format(app['tasksHealthy'])

    check_health_message()


def test_health_check_works_with_resident_task():
    """Verifies that resident tasks (common for Persistent Volumes) do not fail health checks.
       Marathon bug: https://jira.mesosphere.com/browse/MARATHON-7050
    """

    app_def = apps.resident_docker_app()
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id, max_attempts=500)
    tasks = client.get_tasks(app_def["id"])
    assert len(tasks) == 1, "The number of tasks is {}, but 1 was expected".format(len(tasks))

    assert_that(lambda: client.get_app(app_def['id']), eventually(has_value('tasksHealthy', 1), max_attempts=30))


@shakedown.private_agents(2)
def test_pinned_task_scales_on_host_only():
    """Tests that a pinned app scales only on the pinned node."""

    app_def = apps.sleep_app()
    app_id = app_def["id"]
    host = common.ip_other_than_mom()
    common.pin_to_host(app_def, host)

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 1, "The number of tasks is {} after deployment, but 1 was expected".format(len(tasks))
    assert tasks[0]['host'] == host, \
        "The task is on {}, but it is supposed to be on {}".format(tasks[0]['host'], host)

    client.scale_app(app_id, 10)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 10, "The number of tasks is {} after scale, but 10 was expected".format(len(tasks))
    for task in tasks:
        assert task['host'] == host, "The task is on {}, but it is supposed to be on {}".format(task['host'], host)


@shakedown.private_agents(2)
def test_pinned_task_recovers_on_host():
    """Tests that when a pinned task gets killed, it recovers on the node it was pinned to."""

    app_def = apps.sleep_app()
    app_id = app_def["id"]
    host = common.ip_other_than_mom()
    common.pin_to_host(app_def, host)

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)
    tasks = client.get_tasks(app_id)

    common.kill_process_on_host(host, '[s]leep')
    common.deployment_wait(service_id=app_id)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_for_new_task():
        new_tasks = client.get_tasks(app_id)
        assert tasks[0]['id'] != new_tasks[0]['id'], "The task did not get killed: {}".format(tasks[0]['id'])
        assert new_tasks[0]['host'] == host, \
            "The task got restarted on {}, but it was supposed to stay on {}".format(new_tasks[0]['host'], host)

    check_for_new_task()


@shakedown.private_agents(2)
def test_pinned_task_does_not_scale_to_unpinned_host():
    """Tests when a task lands on a pinned node (and barely fits) and it is asked to scale past
       the resources of that node, no tasks will be launched on any other node.
    """

    app_def = apps.sleep_app()
    app_id = app_def['id']

    host = common.ip_other_than_mom()
    print('Constraint set to host: {}'.format(host))
    # the size of cpus is designed to be greater than 1/2 of a node
    # such that only 1 task can land on the node.
    cores = common.cpus_on_agent(host)
    app_def['cpus'] = max(0.6, cores - 0.5)
    common.pin_to_host(app_def, host)

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)
    client.scale_app(app_id, 2)

    time.sleep(5)
    deployments = client.get_deployments(app_id=app_id)
    tasks = client.get_tasks(app_id)

    # still deploying
    assert len(deployments) == 1, "The number of deployments is {}, but 1 was expected".format(len(deployments))
    assert len(tasks) == 1, "The number of tasks is {}, but 1 was expected".format(len(tasks))


@shakedown.private_agents(2)
def test_pinned_task_does_not_find_unknown_host():
    """Tests that a task pinned to an unknown host will not launch.
       Within 10 secs it should still be in deployment and 0 tasks should be running.
    """

    app_def = apps.sleep_app()
    common.pin_to_host(app_def, '10.255.255.254')

    client = marathon.create_client()
    client.add_app(app_def)

    # apps deploy within secs
    # assuming that after 10 no tasks meets criteria
    time.sleep(10)

    tasks = client.get_tasks(app_def["id"])
    assert len(tasks) == 0, "The number of tasks is {}, 0 was expected".format(len(tasks))


@shakedown.dcos_1_8
def test_restart_container_with_persistent_volume():
    """A task with a persistent volume, which writes to a file in the persistent volume, is launched.
       The app is killed and restarted and we can still read from the persistent volume what was written to it.
    """

    app_def = apps.persistent_volume_app()
    app_id = app_def['id']

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 1, "The number of tasks is {} after deployment, but 1 was expected".format(len(tasks))

    host = tasks[0]['host']
    port = tasks[0]['ports'][0]
    cmd = "curl {}:{}/data/foo".format(host, port)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_task(cmd, target_data):
        run, data = shakedown.run_command_on_master(cmd)

        assert run, "{} did not succeed".format(cmd)
        assert data == target_data, "'{}' was not equal to {}".format(data, target_data)

    check_task(cmd, target_data='hello\n')

    client.restart_app(app_id)
    common.deployment_wait(service_id=app_id)

    assert_that(lambda: client.get_tasks(app_id), eventually(has_len(equal_to(1)), max_attempts=30))

    host = tasks[0]['host']
    port = tasks[0]['ports'][0]
    cmd = "curl {}:{}/data/foo".format(host, port)

    check_task(cmd, target_data='hello\nhello\n')


@shakedown.dcos_1_8
def test_app_with_persistent_volume_recovers():
    """Tests that when an app task with a persistent volume gets killed,
       it recovers on the node it was launched on, and it gets attached
       to the same persistent-volume."""

    app_def = apps.persistent_volume_app()
    app_id = app_def['id']

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 1, "The number of tasks is {} after deployment, but 1 was expected".format(len(tasks))

    task_id = tasks[0]['id']
    port = tasks[0]['ports'][0]
    host = tasks[0]['host']
    cmd = "curl {}:{}/data/foo".format(host, port)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_task(cmd, target_data):
        run, data = shakedown.run_command_on_master(cmd)

        assert run, "{} did not succeed".format(cmd)
        assert target_data in data, "'{}' not found in {}".format(target_data, data)

    check_task(cmd, target_data='hello\n')

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def kill_task(host, pattern):
        pids = common.kill_process_on_host(host, pattern)
        assert len(pids) != 0, "no task got killed on {} for pattern {}".format(host, pattern)

    kill_task(host, '[h]ttp\\.server')

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_task_recovery():
        tasks = client.get_tasks(app_id)
        assert len(tasks) == 1, "The number of tasks is {} after recovery, but 1 was expected".format(len(tasks))

        new_task_id = tasks[0]['id']
        assert task_id != new_task_id, "The task ID has not changed, and is still {}".format(task_id)

    check_task_recovery()

    port = tasks[0]['ports'][0]
    host = tasks[0]['host']
    cmd = "curl {}:{}/data/foo".format(host, port)

    check_task(cmd, target_data='hello\nhello\n')


def test_app_update():
    """Tests that an app gets successfully updated."""

    app_def = apps.mesos_app(app_id='/update-app')
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 1, "The number of tasks is {} after deployment, but 1 was expected".format(len(tasks))

    app_def['cpus'] = 1
    app_def['instances'] = 2

    client.update_app(app_id, app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 2, "The number of tasks is {} after deployment, but 2 was expected".format(len(tasks))


def test_app_update_rollback():
    """Tests that an updated app can be rolled back to its initial version."""

    app_def = apps.readiness_and_health_app()
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 1, "The number of tasks is {} after deployment, but 1 was expected".format(len(tasks))

    app_def['instances'] = 2
    client.update_app(app_id, app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 2, "The number of tasks is {} after update, but 2 was expected".format(len(tasks))

    # provides a testing delay to rollback in the meantime
    app_def['readinessChecks'][0]['intervalSeconds'] = 30
    app_def['instances'] = 1
    deployment_id = client.update_app(app_id, app_def)
    client.rollback_deployment(deployment_id)
    common.deployment_wait(service_id=app_id)

    # update to 1 instance is rollback to 2
    tasks = client.get_tasks(app_id)
    assert len(tasks) == 2, "The number of tasks is {} after rollback, but 2 was expected".format(len(tasks))


def test_unhealthy_app_can_be_rolled_back():
    """Verifies that an updated app gets rolled back due to being unhealthy."""

    app_def = apps.readiness_and_health_app()
    app_id = app_def["id"]

    @retrying.retry(
        wait_fixed=1000,
        stop_max_attempt_number=30,
        retry_on_exception=common.ignore_provided_exception(DCOSException)
    )
    def wait_for_deployment():
        common.deployment_wait(service_id=app_id)

    client = marathon.create_client()
    client.add_app(app_def)
    wait_for_deployment()

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 1, "The number of tasks is {} after deployment, but 1 was expected".format(len(tasks))

    app_def['healthChecks'][0]['path'] = '/non-existent'
    app_def['instances'] = 2
    deployment_id = client.update_app(app_id, app_def)

    try:
        wait_for_deployment()
    except Exception:
        client.rollback_deployment(deployment_id)
        wait_for_deployment()

    tasks = client.get_tasks(app_id)
    assert len(tasks) == 1, "The number of tasks is {} after rollback, but 1 was expected".format(len(tasks))


@shakedown.private_agents(2)
def test_marathon_with_master_process_failure(marathon_service_name):
    """Launches an app and restarts the master. It is expected that the service endpoint eventually comes back and
       the task ID stays the same.
    """

    app_def = apps.sleep_app()
    app_id = app_def["id"]

    host = common.ip_other_than_mom()
    common.pin_to_host(app_def, host)

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    original_task_id = tasks[0]['id']

    common.systemctl_master('restart')
    common.wait_for_service_endpoint(marathon_service_name, path="ping")

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_task_recovery():
        tasks = client.get_tasks(app_id)
        assert len(tasks) == 1, "The number of tasks is {} after master restart, but 1 was expected".format(len(tasks))
        assert tasks[0]['id'] == original_task_id, \
            "Task {} has not recovered, it got replaced with another one: {}".format(original_task_id, tasks[0]['id'])

    check_task_recovery()


@shakedown.private_agents(2)
def test_marathon_when_disconnected_from_zk():
    """Launches an app from Marathon, then knocks out access to ZK from Marathon.
       Verifies the task is preserved.
    """

    app_def = apps.sleep_app()
    app_id = app_def["id"]

    host = common.ip_other_than_mom()
    common.pin_to_host(app_def, host)

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)
    tasks = client.get_tasks(app_id)
    original_task_id = tasks[0]['id']

    common.block_iptable_rules_for_seconds(host, 2181, sleep_seconds=10, block_input=True, block_output=False)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_task_is_back():
        tasks = client.get_tasks(app_id)
        assert tasks[0]['id'] == original_task_id, \
            "The task {} got replaced with {}".format(original_task_id, tasks[0]['id'])

    check_task_is_back()


@shakedown.private_agents(2)
def test_marathon_when_task_agent_bounced():
    """Launch an app and restart the node the task is running on."""

    app_def = apps.sleep_app()
    app_id = app_def["id"]

    host = common.ip_other_than_mom()
    common.pin_to_host(app_def, host)

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)
    tasks = client.get_tasks(app_id)
    original_task_id = tasks[0]['id']
    shakedown.restart_agent(host)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def check_task_is_back():
        tasks = client.get_tasks(app_id)
        assert tasks[0]['id'] == original_task_id, \
            "The task {} got replaced with {}".format(original_task_id, tasks[0]['id'])

    check_task_is_back()


def test_default_user():
    """Ensures a task is started as root by default."""

    app_def = apps.sleep_app()
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    app = client.get_app(app_id)
    user = app.get('user')
    assert user is None, "User is {}, but it should not have been set".format(user)

    tasks = client.get_tasks(app_id)
    host = tasks[0]['host']

    success = shakedown.run_command_on_agent(host, "ps aux | grep '[s]leep ' | awk '{if ($1 !=\"root\") exit 1;}'")
    assert success, "The app is running as non-root"


@common.marathon_1_4
def test_declined_offer_due_to_resource_role():
    """Tests that an offer gets declined because the role doesn't exist."""

    app_def = apps.sleep_app()
    app_def["acceptedResourceRoles"] = ["very_random_role"]
    _test_declined_offer(app_def, 'UnfulfilledRole')


@common.marathon_1_4
def test_declined_offer_due_to_cpu_requirements():
    """Tests that an offer gets declined because the number of CPUs can't be found in an offer."""

    app_def = apps.sleep_app()
    app_def["cpus"] = 12345
    _test_declined_offer(app_def, 'InsufficientCpus')


def _test_declined_offer(app_def, reason):
    """Used to confirm that offers were declined.   The `processedOffersSummary` and these tests
       in general require 1.4+ marathon with the queue end point.
       The retry is the best possible way to "time" the success of the test.
    """

    app_id = app_def["id"]
    client = marathon.create_client()
    client.add_app(app_def)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def verify_declined_offer():
        deployments = client.get_deployments(app_id)
        assert len(deployments) == 1

        offer_summary = client.get_queued_app(app_id)['processedOffersSummary']
        role_summary = declined_offer_by_reason(offer_summary['rejectSummaryLastOffers'], reason)
        last_attempt = declined_offer_by_reason(offer_summary['rejectSummaryLaunchAttempt'], reason)

        assert role_summary['declined'] > 0, "There are no declined offers because of {}".format(reason)
        assert role_summary['processed'] > 0, "There are no processed offers for {}".format(reason)
        assert last_attempt['declined'] > 0, "There are no declined offers because of {}".format(reason)
        assert last_attempt['processed'] > 0, "There are no processed offers for {}".format(reason)

    verify_declined_offer()


def declined_offer_by_reason(offers, reason):
    for offer in offers:
        if offer['reason'] == reason:
            del offer['reason']
            return offer

    return None


@pytest.mark.skipif("common.docker_env_not_set()")
def test_private_repository_docker_app():
    username = os.environ['DOCKER_HUB_USERNAME']
    password = os.environ['DOCKER_HUB_PASSWORD']
    agents = shakedown.get_private_agents()

    common.create_docker_credentials_file(username, password)
    common.copy_docker_credentials_file(agents)

    app_def = apps.private_docker_app()
    app_id = app_def["id"]

    if shakedown.ee_version() == 'strict':
        app_def['user'] = 'root'
        common.add_dcos_marathon_user_acls()

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    common.assert_app_tasks_running(client, app_def)


def test_ping(marathon_service_name):
    """Tests the Marathon's /ping end-point."""
    response = common.http_get_marathon_path('ping', marathon_service_name)
    assert response.status_code == 200, "HTTP status code {} is NOT 200".format(response.status_code)
    assert 'pong' in response.text, "Got {} instead of pong".format(response.text)


def test_metrics_endpoint(marathon_service_name):
    service_url = shakedown.dcos_service_url(marathon_service_name)
    response = http.get("{}metrics".format(service_url))
    assert response.status_code == 200, "HTTP status code {} is NOT 200".format(response.status_code)

    if marthon_version_less_than('1.7'):
        metric_name = 'service.mesosphere.marathon.app.count'
    else:
        metric_name = 'marathon.apps.active.gauge'

    response_json = response.json()
    print(response_json['gauges'])
    assert response_json['gauges'][metric_name] is not None, \
        "{} is absent".format(metric_name)


def test_healtchcheck_and_volume():
    """Launches a Docker container on Marathon."""

    app_def = apps.healthcheck_and_volume()
    app_id = app_def["id"]

    client = marathon.create_client()
    client.add_app(app_def)
    common.deployment_wait(service_id=app_id)

    tasks = client.get_tasks(app_id)
    app = client.get_app(app_id)

    assert len(tasks) == 1, "The number of tasks is {} after deployment, but only 1 was expected".format(len(tasks))
    assert len(app['container']['volumes']) == 2, "The container does not have the correct amount of volumes"

    # check if app becomes healthy
    assert_that(lambda: client.get_app(app_id), eventually(has_value('tasksHealthy', 1), max_attempts=30))


@shakedown.dcos_1_9
def test_vip_mesos_cmd(marathon_service_name):
    """Validates the creation of an app with a VIP label and the accessibility of the service via the VIP."""

    app_def = apps.http_server()
    app_id = app_def["id"]

    vip_name = app_id.lstrip("/")
    fqn = '{}.{}.l4lb.thisdcos.directory'.format(vip_name, marathon_service_name)

    app_def['portDefinitions'] = [{
        "port": 0,
        "protocol": "tcp",
        "name": "{}".format(vip_name),
        "labels": {
            "VIP_0": "/{}:10000".format(vip_name)
        }
    }]

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def http_output_check():
        time.sleep(1)
        common.assert_http_code('{}:{}'.format(fqn, 10000))

    http_output_check()


@shakedown.dcos_1_9
def test_vip_docker_bridge_mode(marathon_service_name):
    """Tests the creation of a VIP from a python command in a docker image using bridge mode.
       the test validates the creation of an app with the VIP label and the accessability
       of the service via the VIP.
    """

    app_def = apps.docker_http_server(app_id='vip-docker-bridge-mode-app')
    app_id = app_def["id"]

    vip_name = app_id.lstrip("/")
    fqn = '{}.{}.l4lb.thisdcos.directory'.format(vip_name, marathon_service_name)

    app_def['id'] = vip_name
    app_def['container']['docker']['portMappings'] = [{
        "containerPort": 8080,
        "hostPort": 0,
        "labels": {
            "VIP_0": "/{}:10000".format(vip_name)
        },
        "protocol": "tcp",
        "name": "{}".format(vip_name)
    }]

    client = marathon.create_client()
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=30, retry_on_exception=common.ignore_exception)
    def http_output_check():
        time.sleep(1)
        common.assert_http_code('{}:{}'.format(fqn, 10000))

    http_output_check()


def requires_marathon_version(version):
    """This python module is for testing root and MoM marathons.   The @marathon_1_5
       annotation works only for the root marathon.   The context switching necessary
       for switching the marathons occurs after the evaluation of the pytestmark.
       This function is used to ensure the correct version of marathon regardless
       of root or mom.
    """
    # marathon version captured here will work for root and mom
    if shakedown.marthon_version_less_than(version):
        pytest.skip()


@pytest.mark.parametrize("test_type, get_pinger_app, dns_format", [
    ('localhost', apps.pinger_localhost_app, '{}.{}.mesos'),
    ('bridge', apps.pinger_bridge_app, '{}.{}.mesos'),
    ('container', apps.pinger_container_app, '{}.{}.containerip.dcos.thisdcos.directory'),
])
@shakedown.dcos_1_9
@shakedown.private_agents(2)
def test_network_pinger(test_type, get_pinger_app, dns_format, marathon_service_name):
    """This test runs a pinger app and a relay app. It retrieves the python app from the
       master via the new http service (which will be moving into shakedown). Then a curl call
       to the relay will invoke a call to the 2nd pinger app and return back pong to the relay
       then back to curl.

       It tests that 1 task can network communicate to another task on the given network
       It tests inbound and outbound connectivity

       test_type param is not used.  It is passed so that it is clear which parametrized test
       is running or may be failing.
    """
    pinger_app = get_pinger_app()
    relay_app = get_pinger_app()
    relay_app["id"] = relay_app["id"].replace("pinger", "relay")
    pinger_dns = dns_format.format(pinger_app["id"].lstrip("/"), marathon_service_name)
    relay_dns = dns_format.format(relay_app["id"].lstrip("/"), marathon_service_name)

    # test pinger app to master
    shakedown.copy_file_to_master(os.path.join(scripts.scripts_dir(), "pinger.py"))

    client = marathon.create_client()

    with shakedown.master_http_service():
        # need to add app with http service in place or it will fail to fetch
        client.add_app(pinger_app)
        client.add_app(relay_app)
        common.deployment_wait(service_id=pinger_app["id"])
        common.deployment_wait(service_id=relay_app["id"])
        shakedown.wait_for_dns(relay_dns)

    relay_url = 'http://{}:7777/relay-ping?url={}:7777'.format(relay_dns, pinger_dns)

    @retrying.retry(wait_fixed=1000, stop_max_attempt_number=300, retry_on_exception=common.ignore_exception)
    def http_output_check():
        status, output = shakedown.run_command_on_master('curl {}'.format(relay_url))
        assert status, "curl {} failed on master with {}".format(relay_url, output)
        assert 'Pong {}'.format(pinger_app["id"]) in output
        assert 'Relay from {}'.format(relay_app["id"]) in output

    http_output_check()


@shakedown.dcos_1_11
def test_ipv6_healthcheck(docker_ipv6_network_fixture):
    """ There is new feature in DC/OS 1.11 that allows containers running on IPv6 network to be healthchecked from
        Marathon. This tests verifies executing such healthcheck.
    """
    app_def = apps.ipv6_healthcheck()
    app_id = app_def["id"]
    client = marathon.create_client()
    target_instances_count = app_def['instances']
    client.add_app(app_def)

    common.deployment_wait(service_id=app_id)

    app = client.get_app(app_id)
    assert app['tasksRunning'] == target_instances_count, \
        "The number of running tasks is {}, but {} was expected".format(app['tasksRunning'], target_instances_count)
    assert app['tasksHealthy'] == target_instances_count, \
        "The number of healthy tasks is {}, but {} was expected".format(app['tasksHealthy'], target_instances_count)

    client.remove_app(app['id'], True)


@shakedown.dcos_1_11
@pytest.mark.skipif("shakedown.ee_version() is None")
@pytest.mark.usefixtures("wait_for_marathon_and_cleanup")
def test_faultdomains_default():
    """Tests if the applications that do not have a fault domain specified are launched on the same
       fault domain as the master.
    """

    # Discover which is the "local" cluster domain
    local_domain = get_cluster_local_domain()

    # Get how many agents do we have in the local domain (zone and region) and try to start
    # that many applications. Mesos should round-robbin the instances in the region
    region_agents = count_agents_in_faultdomains(regions=local_domain.region)

    # Create an app with default configuration
    app_def = apps.faultdomain_app(instances=region_agents, suffix="-defaults")
    client = marathon.create_client()
    client.add_app(app_def)

    # Wait for the app to be deployed
    common.deployment_wait(app_id=app_def['id'])

    # Ensure that all instances are running in the same region (it's ok if they belong to different zones)
    app = client.get_app(app_def['id'])
    common.assert_app_in_all_domains(app, regions=(local_domain.region,))


@shakedown.dcos_1_11
@pytest.mark.skipif("shakedown.ee_version() is None")
@pytest.mark.usefixtures("wait_for_marathon_and_cleanup")
def test_faultdomains_region_only():
    """Tests if the applications that only have a `region` specified are launched on all the different
       zones available in that region on the cluster.
    """

    # Find out the biggest region in the cluster
    (region, zones) = get_biggest_cluster_region()

    assert len(zones) > 1, "The tests require at least 2 zones in the biggest region ({})".format(
        region,
    )

    # Create an app pinned on the given region, with at least double the number of instances than
    # the zones in the cluster, to ensure that they are distributed among all of them
    app_def = apps.faultdomain_app(region=region, instances=len(zones) * 2, suffix="-region-only")
    client = marathon.create_client()
    client.add_app(app_def)

    # Wait for the app to be deployed
    common.deployment_wait(app_id=app_def['id'])

    # Ensure that all instances are running in the same region, and on *all* the region zones
    app = client.get_app(app_def['id'])
    common.assert_app_in_all_domains(app, regions=(region,), zones=zones)


@shakedown.dcos_1_11
@pytest.mark.skipif("shakedown.ee_version() is None")
@pytest.mark.usefixtures("wait_for_marathon_and_cleanup")
def test_faultdomains_region_and_zone():
    """Tests if the applications with a `region` and a `zone` defined are always starting on the
       same region and zone
    """

    # Find out the biggest region in the cluster
    (region, zones) = get_biggest_cluster_region()

    assert len(zones) > 1, "The tests require at least 2 zones in the biggest region ({})".format(
        region,
    )

    single_zone = zones[0]

    # Create an app pinned on the given region and zone.
    #
    # (We are starting more instances than the number of zones in the region in order to increase
    #  the chance of a faulty instance to appear on a different zone)
    #
    app_def = apps.faultdomain_app(
        region=region,
        zone=single_zone,
        instances=len(zones) * 2,
        suffix="-region-and-zone")
    client = marathon.create_client()
    client.add_app(app_def)

    # Wait for the app to be deployed
    common.deployment_wait(app_id=app_def['id'])

    # Ensure that all instances are running in the same region, and on *all* the region zones
    app = client.get_app(app_def['id'])
    common.assert_app_in_all_domains(app, regions=(region,), zones=(single_zone,))


@shakedown.dcos_1_11
@pytest.mark.skipif("shakedown.ee_version() is None")
@pytest.mark.usefixtures("wait_for_marathon_and_cleanup")
def test_faultdomains_region_unique():
    """Tests if the applications with a ["@region", "UNIQUE"] constraint correctly starts the apps
       across all regions in the cluster
    """

    # Find out all the regions in the cluster
    regions = set(get_all_cluster_regions().keys())

    assert len(regions) > 1, "The tests require at least 2 regions in the cluster"

    # Create an app that should be deployed on a unique region
    # The number of instances should be the number of regions available in the system
    app_def = apps.faultdomain_app(
        instances=len(regions),
        constraints=[["@region", "UNIQUE"]],
        suffix="-region-unique"
    )
    client = marathon.create_client()
    client.add_app(app_def)

    # Wait for the app to be deployed
    common.deployment_wait(app_id=app_def['id'])

    # Check if the application was launched correctly
    app = client.get_app(app_def['id'])
    (used_regions, used_zones) = get_used_regions_and_zones(get_app_domains(app))

    assert_that(used_regions, equal_to(regions), "App {} was not launched in all regions: {} instead of {}".format(
            app['id'], ', '.join(used_regions), ', '.join(regions)
        ))


@shakedown.dcos_1_11
@pytest.mark.skipif("shakedown.ee_version() is None")
@pytest.mark.usefixtures("wait_for_marathon_and_cleanup")
def test_faultdomains_zone_unique():
    """Tests if the applications with a region fixed and a ["@zone", "UNIQUE"] constraint correctly starts
       the apps across all zones in the given region
    """

    # Find out the biggest region in the cluster
    (region, zones) = get_biggest_cluster_region()

    assert len(zones) > 1, "The tests require at least 2 zones in the biggest region ({})".format(
        region,
    )

    # Create an app that should be deployed on a unique region
    # The number of instances should be the number of regions available in the system
    app_def = apps.faultdomain_app(
        region=region,
        instances=len(zones),
        constraints=[["@zone", "UNIQUE"]],
        suffix="-zone-unique"
    )
    client = marathon.create_client()
    client.add_app(app_def)

    # Wait for the app to be deployed
    common.deployment_wait(app_id=app_def['id'])

    # Check if the application was launched correctly
    app = client.get_app(app_def['id'])
    (used_regions, used_zones) = get_used_regions_and_zones(get_app_domains(app))

    assert_that(used_regions, equal_to(set([region])),
                "App {} was not launched in the expected region(s): {} instead of {}".format(
                    app['id'], ', '.join(used_regions), region
                ))
    assert_that(used_zones, equal_to(set(zones)),
                "App {} was not launched in all zones: {} instead of {}".format(
                    app['id'], ', '.join(used_zones), ', '.join(zones)
                ))
