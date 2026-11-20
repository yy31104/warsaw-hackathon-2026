from .call_webhook import Webhook
from .escalate import Escalate
from .http_get import HealthCheck
from .open_ticket import Ticket
from .restart_service import Restart
from .retry_workflow import Retry

REGISTRY = {a.name: a for a in [HealthCheck(), Retry(), Restart(), Webhook(), Ticket(), Escalate()]}
