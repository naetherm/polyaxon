import logging
import mimetypes
import os

from hestia.bool_utils import to_bool
from wsgiref.util import FileWrapper

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import (
    CreateAPIView,
    RetrieveAPIView,
    RetrieveUpdateDestroyAPIView,
    get_object_or_404
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings

from django.http import StreamingHttpResponse

import auditor

from api.filters import OrderingFilter, QueryFilter
from api.jobs import queries
from api.jobs.serializers import (
    BookmarkedJobSerializer,
    JobCreateSerializer,
    JobDetailSerializer,
    JobSerializer,
    JobStatusSerializer
)
from api.utils.views.auditor_mixin import AuditorMixinView
from api.utils.views.list_create import ListCreateAPIView
from api.utils.views.post import PostAPIView
from api.utils.views.protected import ProtectedView
from db.models.jobs import Job, JobStatus
from db.redis.heartbeat import RedisHeartBeat
from db.redis.tll import RedisTTL
from event_manager.events.job import (
    JOB_CREATED,
    JOB_DELETED_TRIGGERED,
    JOB_LOGS_VIEWED,
    JOB_OUTPUTS_DOWNLOADED,
    JOB_RESTARTED_TRIGGERED,
    JOB_STATUSES_VIEWED,
    JOB_STOPPED_TRIGGERED,
    JOB_UPDATED,
    JOB_VIEWED
)
from event_manager.events.project import PROJECT_JOBS_VIEWED
from libs.archive import archive_job_outputs
from libs.authentication.internal import InternalAuthentication
from libs.paths.jobs import get_job_logs_path
from libs.permissions.internal import IsAuthenticatedOrInternal
from libs.permissions.projects import get_permissible_project
from libs.spec_validation import validate_job_spec_config
from polyaxon.celery_api import celery_app
from polyaxon.settings import SchedulerCeleryTasks

_logger = logging.getLogger("polyaxon.views.jobs")


class ProjectJobListView(ListCreateAPIView):
    """
    get:
        List jobs under a project.

    post:
        Create a job under a project.
    """
    queryset = queries.jobs
    serializer_class = BookmarkedJobSerializer
    create_serializer_class = JobCreateSerializer
    permission_classes = (IsAuthenticated,)
    filter_backends = (QueryFilter, OrderingFilter,)
    query_manager = 'job'
    ordering = ('-updated_at',)
    ordering_fields = ('created_at', 'updated_at', 'started_at', 'finished_at')

    def filter_queryset(self, queryset):
        project = get_permissible_project(view=self)
        auditor.record(event_type=PROJECT_JOBS_VIEWED,
                       instance=project,
                       actor_id=self.request.user.id,
                       actor_name=self.request.user.username)
        queryset = queryset.filter(project=project)
        return super().filter_queryset(queryset=queryset)

    def perform_create(self, serializer):
        ttl = self.request.data.get(RedisTTL.TTL_KEY)
        if ttl:
            try:
                ttl = RedisTTL.validate_ttl(ttl)
            except ValueError:
                raise ValidationError('ttl must be an integer.')
        instance = serializer.save(user=self.request.user,
                                   project=get_permissible_project(view=self))
        auditor.record(event_type=JOB_CREATED, instance=instance)
        if ttl:
            RedisTTL.set_for_job(job_id=instance.id, value=ttl)


class JobDetailView(AuditorMixinView, RetrieveUpdateDestroyAPIView):
    """
    get:
        Get a job details.
    patch:
        Update a job details.
    delete:
        Delete a job.
    """
    queryset = queries.jobs_details
    serializer_class = JobDetailSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    instance = None
    get_event = JOB_VIEWED
    update_event = JOB_UPDATED
    delete_event = JOB_DELETED_TRIGGERED

    def filter_queryset(self, queryset):
        return queryset.filter(project=get_permissible_project(view=self))


class JobCloneView(CreateAPIView):
    queryset = Job.objects.all()
    serializer_class = JobSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    event_type = None

    def filter_queryset(self, queryset):
        return queryset.filter(project=get_permissible_project(view=self))

    def clone(self, obj, config, update_code_reference, description):
        pass

    def post(self, request, *args, **kwargs):
        ttl = self.request.data.get(RedisTTL.TTL_KEY)
        if ttl:
            try:
                ttl = RedisTTL.validate_ttl(ttl)
            except ValueError:
                raise ValidationError('ttl must be an integer.')

        obj = self.get_object()
        auditor.record(event_type=self.event_type,
                       instance=obj,
                       actor_id=self.request.user.id,
                       actor_name=self.request.user.username)

        description = None
        config = None
        update_code_reference = False
        if 'config' in request.data:
            spec = validate_job_spec_config(
                [obj.specification.parsed_data, request.data['config']], raise_for_rest=True)
            config = spec.parsed_data
        if 'update_code' in request.data:
            try:
                update_code_reference = to_bool(request.data['update_code'])
            except TypeError:
                raise ValidationError('update_code should be a boolean')
        if 'description' in request.data:
            description = request.data['description']
        new_obj = self.clone(obj=obj,
                             config=config,
                             update_code_reference=update_code_reference,
                             description=description)
        if ttl:
            RedisTTL.set_for_job(job_id=new_obj.id, value=ttl)
        serializer = self.get_serializer(new_obj)
        return Response(status=status.HTTP_201_CREATED, data=serializer.data)


class JobRestartView(JobCloneView):
    """Restart a job."""
    queryset = Job.objects.all()
    serializer_class = JobSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'
    event_type = JOB_RESTARTED_TRIGGERED

    def clone(self, obj, config, update_code_reference, description):
        return obj.restart(user=self.request.user,
                           config=config,
                           update_code_reference=update_code_reference,
                           description=description)


class JobViewMixin(object):
    """A mixin to filter by job."""
    project = None
    job = None

    def get_job(self):
        # Get project and check access
        self.project = get_permissible_project(view=self)
        self.job = get_object_or_404(Job, project=self.project, id=self.kwargs['job_id'])
        return self.job

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)
        return queryset.filter(job=self.get_job())


class JobStatusListView(JobViewMixin, ListCreateAPIView):
    """
    get:
        List all statuses of a job.
    post:
        Create a job status.
    """
    queryset = JobStatus.objects.order_by('created_at').all()
    serializer_class = JobStatusSerializer
    permission_classes = (IsAuthenticated,)

    def perform_create(self, serializer):
        serializer.save(job=self.get_job())

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        auditor.record(event_type=JOB_STATUSES_VIEWED,
                       instance=self.job,
                       actor_id=request.user.id,
                       actor_name=request.user.username)
        return response


class JobStatusDetailView(JobViewMixin, RetrieveAPIView):
    """Get job status details."""
    queryset = JobStatus.objects.all()
    serializer_class = JobStatusSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'uuid'


class JobLogsView(JobViewMixin, RetrieveAPIView):
    """Get job logs."""
    permission_classes = (IsAuthenticated,)

    def get(self, request, *args, **kwargs):
        job = self.get_job()
        auditor.record(event_type=JOB_LOGS_VIEWED,
                       instance=self.job,
                       actor_id=request.user.id,
                       actor_name=request.user.username)
        log_path = get_job_logs_path(job.unique_name)

        filename = os.path.basename(log_path)
        chunk_size = 8192
        try:
            wrapped_file = FileWrapper(open(log_path, 'rb'), chunk_size)
            response = StreamingHttpResponse(wrapped_file,
                                             content_type=mimetypes.guess_type(log_path)[0])
            response['Content-Length'] = os.path.getsize(log_path)
            response['Content-Disposition'] = "attachment; filename={}".format(filename)
            return response
        except FileNotFoundError:
            _logger.warning('Log file not found: log_path=%s', log_path)
            return Response(status=status.HTTP_404_NOT_FOUND,
                            data='Log file not found: log_path={}'.format(log_path))


class JobStopView(CreateAPIView):
    """Stop a job."""
    queryset = Job.objects.all()
    serializer_class = JobSerializer
    permission_classes = (IsAuthenticated,)
    lookup_field = 'id'

    def filter_queryset(self, queryset):
        return queryset.filter(project=get_permissible_project(view=self))

    def post(self, request, *args, **kwargs):
        obj = self.get_object()
        auditor.record(event_type=JOB_STOPPED_TRIGGERED,
                       instance=obj,
                       actor_id=request.user.id,
                       actor_name=request.user.username)
        celery_app.send_task(
            SchedulerCeleryTasks.JOBS_STOP,
            kwargs={
                'project_name': obj.project.unique_name,
                'project_uuid': obj.project.uuid.hex,
                'job_name': obj.unique_name,
                'job_uuid': obj.uuid.hex,
                'specification': obj.specification,
                'update_status': True
            })
        return Response(status=status.HTTP_200_OK)


class DownloadOutputsView(ProtectedView):
    """Download outputs of a job."""
    permission_classes = (IsAuthenticated,)
    HANDLE_UNAUTHENTICATED = False

    def get_object(self):
        project = get_permissible_project(view=self)
        job = get_object_or_404(Job, project=project, id=self.kwargs['id'])
        auditor.record(event_type=JOB_OUTPUTS_DOWNLOADED,
                       instance=job,
                       actor_id=self.request.user.id,
                       actor_name=self.request.user.username)
        return job

    def get(self, request, *args, **kwargs):
        job = self.get_object()
        archived_path, archive_name = archive_job_outputs(
            persistence_outputs=job.persistence_outputs,
            job_name=job.unique_name)
        return self.redirect(path='{}/{}'.format(archived_path, archive_name))


class JobHeartBeatView(PostAPIView):
    """
    post:
        Post a heart beat ping.
    """
    queryset = Job.objects.all()
    permission_classes = (IsAuthenticatedOrInternal,)
    authentication_classes = api_settings.DEFAULT_AUTHENTICATION_CLASSES + [
        InternalAuthentication,
    ]
    lookup_field = 'id'

    def post(self, request, *args, **kwargs):
        job = self.get_object()
        RedisHeartBeat.job_ping(job_id=job.id)
        return Response(status=status.HTTP_200_OK)
