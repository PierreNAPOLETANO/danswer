import time
from typing import cast

from danswer.configs.constants import DocumentSource
from danswer.connectors.factory import build_connector
from danswer.connectors.models import InputType
from danswer.connectors.slack.config import get_pull_frequency
from danswer.connectors.slack.pull import PeriodicSlackLoader
from danswer.connectors.web.pull import WebLoader
from danswer.db.index_attempt import fetch_index_attempts
from danswer.db.index_attempt import update_index_attempt
from danswer.db.models import IndexingStatus
from danswer.dynamic_configs import get_dynamic_config_store
from danswer.dynamic_configs.interface import ConfigNotFoundError
from danswer.utils.indexing_pipeline import build_indexing_pipeline
from danswer.utils.logging import setup_logger

logger = setup_logger()

LAST_PULL_KEY_TEMPLATE = "last_pull_{}"


def _check_should_run(current_time: int, last_pull: int, pull_frequency: int) -> bool:
    return current_time - last_pull > pull_frequency * 60


def run_update() -> None:
    logger.info("Running update")
    # TODO (chris): implement a more generic way to run updates
    # so we don't need to edit this file for future connectors
    dynamic_config_store = get_dynamic_config_store()
    indexing_pipeline = build_indexing_pipeline()
    current_time = int(time.time())

    # Slack
    # TODO (chris): make Slack use the same approach as other connectors /
    # make other connectors periodic
    try:
        pull_frequency = get_pull_frequency()
    except ConfigNotFoundError:
        pull_frequency = 0
    if pull_frequency:
        last_slack_pull_key = LAST_PULL_KEY_TEMPLATE.format(
            PeriodicSlackLoader.__name__
        )
        try:
            last_pull = cast(int, dynamic_config_store.load(last_slack_pull_key))
        except ConfigNotFoundError:
            last_pull = None

        if last_pull is None or _check_should_run(
            current_time, last_pull, pull_frequency
        ):
            logger.info(f"Running slack pull from {last_pull or 0} to {current_time}")
            for doc_batch in PeriodicSlackLoader().load(last_pull or 0, current_time):
                indexing_pipeline(doc_batch)
            dynamic_config_store.store(last_slack_pull_key, current_time)

    # TODO (chris): make this more efficient / in a single transaction to
    # prevent race conditions across multiple background jobs. For now,
    # this assumes we only ever run a single background job at a time
    not_started_index_attempts = fetch_index_attempts(
        input_types=[InputType.PULL], statuses=[IndexingStatus.NOT_STARTED]
    )
    for not_started_index_attempt in not_started_index_attempts:
        logger.info(
            "Attempting to index with IndexAttempt id: "
            f"{not_started_index_attempt.id}, source: "
            f"{not_started_index_attempt.source}, input_type: "
            f"{not_started_index_attempt.input_type}, and connector_specific_config: "
            f"{not_started_index_attempt.connector_specific_config}"
        )
        update_index_attempt(
            index_attempt_id=not_started_index_attempt.id,
            new_status=IndexingStatus.IN_PROGRESS,
        )

        error_msg = None
        try:
            # TODO (chris): spawn processes to parallelize / take advantage of
            # multiple cores + implement retries
            connector = build_connector(
                source=not_started_index_attempt.source,
                input_type=InputType.PULL,
                connector_specific_config=not_started_index_attempt.connector_specific_config,
            )

            document_ids: list[str] = []
            for doc_batch in connector.load():
                indexing_pipeline(doc_batch)
                document_ids.extend([doc.id for doc in doc_batch])
        except Exception as e:
            logger.exception(
                "Failed to index for source %s with config %s due to: %s",
                not_started_index_attempt.source,
                not_started_index_attempt.connector_specific_config,
                e,
            )
            error_msg = str(e)

        update_index_attempt(
            index_attempt_id=not_started_index_attempt.id,
            new_status=IndexingStatus.FAILED if error_msg else IndexingStatus.SUCCESS,
            document_ids=document_ids if not error_msg else None,
            error_msg=error_msg,
        )


def update_loop(delay: int = 60) -> None:
    while True:
        start = time.time()
        try:
            run_update()
        except Exception:
            logger.exception("Failed to run update")
        sleep_time = delay - (time.time() - start)
        if sleep_time > 0:
            time.sleep(sleep_time)


if __name__ == "__main__":
    update_loop()