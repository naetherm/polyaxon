import logging
import traceback

from kubernetes.client.rest import ApiException

from django.conf import settings

from constants.jobs import JobLifeCycle
from libs.paths.exceptions import VolumeNotFoundError
from scheduler.spawners.tensorboard_spawner import TensorboardSpawner
from scheduler.spawners.utils import get_job_definition

_logger = logging.getLogger('polyaxon.scheduler.tensorboard')


def start_tensorboard(tensorboard):
    # Update job status to show that its started
    tensorboard.set_status(JobLifeCycle.SCHEDULED)

    spawner = TensorboardSpawner(
        project_name=tensorboard.project.unique_name,
        project_uuid=tensorboard.project.uuid.hex,
        job_name=tensorboard.unique_name,
        job_uuid=tensorboard.uuid.hex,
        k8s_config=settings.K8S_CONFIG,
        namespace=settings.K8S_NAMESPACE,
        in_cluster=True)

    error = {}
    try:
        results = spawner.start_tensorboard(
            image=tensorboard.image,
            outputs_path=tensorboard.outputs_path,
            persistence_outputs=tensorboard.persistence_outputs,
            outputs_refs_jobs=tensorboard.outputs_refs_jobs,
            outputs_refs_experiments=tensorboard.outputs_refs_experiments,
            resources=tensorboard.resources,
            node_selector=tensorboard.node_selector,
            affinity=tensorboard.affinity,
            tolerations=tensorboard.tolerations)
        tensorboard.definition = get_job_definition(results)
        tensorboard.save(update_fields=['definition'])
        return
    except ApiException:
        _logger.error('Could not start tensorboard, please check your polyaxon spec.',
                      exc_info=True)
        error = {
            'raised': True,
            'traceback': traceback.format_exc(),
            'message': 'Could not start the job, encountered a Kubernetes ApiException.',
        }
    except VolumeNotFoundError as e:
        _logger.error('Could not start the tensorboard, please check your volume definitions.',
                      exc_info=True)
        error = {
            'raised': True,
            'traceback': traceback.format_exc(),
            'message': 'Could not start the job, encountered a volume definition problem. %s' % e,
        }
    except Exception as e:
        _logger.error('Could not start tensorboard, please check your polyaxon spec.',
                      exc_info=True)
        error = {
            'raised': True,
            'traceback': traceback.format_exc(),
            'message': 'Could not start tensorboard encountered an {} exception.'.format(
                e.__class__.__name__)
        }
    finally:
        if error.get('raised'):
            tensorboard.set_status(
                JobLifeCycle.FAILED,
                message=error.get('message'),
                traceback=error.get('traceback'))


def stop_tensorboard(project_name,
                     project_uuid,
                     tensorboard_job_name,
                     tensorboard_job_uuid):
    spawner = TensorboardSpawner(
        project_name=project_name,
        project_uuid=project_uuid,
        job_name=tensorboard_job_name,
        job_uuid=tensorboard_job_uuid,
        k8s_config=settings.K8S_CONFIG,
        namespace=settings.K8S_NAMESPACE,
        in_cluster=True)

    return spawner.stop_tensorboard()


def get_tensorboard_url(tensorboard):
    spawner = TensorboardSpawner(
        project_name=tensorboard.project.unique_name,
        project_uuid=tensorboard.project.uuid.hex,
        job_name=tensorboard.unique_name,
        job_uuid=tensorboard.uuid.hex,
        k8s_config=settings.K8S_CONFIG,
        namespace=settings.K8S_NAMESPACE,
        in_cluster=True)
    return spawner.get_tensorboard_url()
