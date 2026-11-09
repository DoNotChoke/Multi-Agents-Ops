"""The Temporal worker for the `investigation` task queue."""

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from opsteam_worker.activities import InvestigationActivities
from opsteam_worker.workflows.investigation import IncidentInvestigationWorkflow
from opsteam_worker.workflows.models import TASK_QUEUE


async def connect(address: str, namespace: str = "default") -> Client:
    """A client that carries pydantic models across workflow and activity boundaries."""
    return await Client.connect(
        address, namespace=namespace, data_converter=pydantic_data_converter
    )


def build_worker(client: Client, activities: InvestigationActivities) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[IncidentInvestigationWorkflow],
        activities=[activities.begin, activities.plan, activities.specialist, activities.conclude],
    )
