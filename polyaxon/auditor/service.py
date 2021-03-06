from auditor.manager import default_manager
from event_manager.event_service import EventService


class AuditorService(EventService):
    """An service that just passes the event to author services."""
    __all__ = EventService.__all__ + ('log', 'notify', 'track')

    event_manager = default_manager

    def __init__(self):
        self.notifier = None
        self.tracker = None
        self.activitylogs = None

    def record_event(self, event):
        """
        Record the event async.
        """
        from polyaxon.celery_api import celery_app
        from polyaxon.settings import EventsCeleryTasks

        event = event.serialize(dumps=False, include_actor_name=True, include_instance_info=True)

        celery_app.send_task(EventsCeleryTasks.EVENTS_TRACK, kwargs={'event': event})
        celery_app.send_task(EventsCeleryTasks.EVENTS_LOG, kwargs={'event': event})
        celery_app.send_task(EventsCeleryTasks.EVENTS_NOTIFY, kwargs={'event': event})

    def notify(self, event):
        self.notifier.record(event_type=event['type'], event_data=event)

    def track(self, event):
        self.tracker.record(event_type=event['type'], event_data=event)

    def log(self, event):
        self.activitylogs.record(event_type=event['type'], event_data=event)

    def setup(self):
        super().setup()
        # Load default event types
        import auditor.events  # noqa

        import notifier
        import activitylogs
        import tracker

        self.notifier = notifier
        self.tracker = tracker
        self.activitylogs = activitylogs
