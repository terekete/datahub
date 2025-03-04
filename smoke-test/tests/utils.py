import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple
from time import sleep
from joblib import Parallel, delayed

import requests_wrapper as requests

from datahub.cli import cli_utils
from datahub.cli.cli_utils import get_system_auth
from datahub.ingestion.run.pipeline import Pipeline

TIME: int = 1581407189000


def get_frontend_session():
    session = requests.Session()

    headers = {
        "Content-Type": "application/json",
    }
    system_auth = get_system_auth()
    if system_auth is not None:
        session.headers.update({"Authorization": system_auth})
    else:
        username, password = get_admin_credentials()
        data = '{"username":"' + username + '", "password":"' + password + '"}'
        response = session.post(
            f"{get_frontend_url()}/logIn", headers=headers, data=data
        )
        response.raise_for_status()

    return session


def get_admin_username() -> str:
    return get_admin_credentials()[0]


def get_admin_credentials():
    return (
        os.getenv("ADMIN_USERNAME", "datahub"),
        os.getenv("ADMIN_PASSWORD", "datahub"),
    )


def get_root_urn():
    return "urn:li:corpuser:datahub"


def get_gms_url():
    return os.getenv("DATAHUB_GMS_URL") or "http://localhost:8080"


def get_frontend_url():
    return os.getenv("DATAHUB_FRONTEND_URL") or "http://localhost:9002"


def get_kafka_broker_url():
    return os.getenv("DATAHUB_KAFKA_URL") or "localhost:9092"


def get_kafka_schema_registry():
    #  internal registry "http://localhost:8080/schema-registry/api/"
    return os.getenv("DATAHUB_KAFKA_SCHEMA_REGISTRY_URL") or "http://localhost:8081"


def get_mysql_url():
    return os.getenv("DATAHUB_MYSQL_URL") or "localhost:3306"


def get_mysql_username():
    return os.getenv("DATAHUB_MYSQL_USERNAME") or "datahub"


def get_mysql_password():
    return os.getenv("DATAHUB_MYSQL_PASSWORD") or "datahub"


def get_sleep_info() -> Tuple[int, int]:
    return (
        int(os.getenv("DATAHUB_TEST_SLEEP_BETWEEN", 20)),
        int(os.getenv("DATAHUB_TEST_SLEEP_TIMES", 3)),
    )


def is_k8s_enabled():
    return os.getenv("K8S_CLUSTER_ENABLED", "false").lower() in ["true", "yes"]


def wait_for_healthcheck_util():
    assert not check_endpoint(f"{get_frontend_url()}/admin")
    assert not check_endpoint(f"{get_gms_url()}/health")


def check_endpoint(url):
    try:
        get = requests.get(url)
        if get.status_code == 200:
            return
        else:
            return f"{url}: is Not reachable, status_code: {get.status_code}"
    except requests.exceptions.RequestException as e:
        raise SystemExit(f"{url}: is Not reachable \nErr: {e}")


def ingest_file_via_rest(filename: str) -> Pipeline:
    pipeline = Pipeline.create(
        {
            "source": {
                "type": "file",
                "config": {"filename": filename},
            },
            "sink": {
                "type": "datahub-rest",
                "config": {"server": get_gms_url()},
            },
        }
    )
    pipeline.run()
    pipeline.raise_from_status()
    sleep(requests.ELASTICSEARCH_REFRESH_INTERVAL_SECONDS)

    return pipeline


def delete_urn(urn: str) -> None:
    payload_obj = {"urn": urn}

    cli_utils.post_delete_endpoint_with_session_and_url(
        requests.Session(),
        get_gms_url() + "/entities?action=delete",
        payload_obj,
    )


def delete_urns(urns: List[str]) -> None:
    for urn in urns:
        delete_urn(urn)


def delete_urns_from_file(filename: str, shared_data: bool = False) -> None:
    if not cli_utils.get_boolean_env_variable("CLEANUP_DATA", True):
        print("Not cleaning data to save time")
        return
    session = requests.Session()
    session.headers.update(
        {
            "X-RestLi-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }
    )

    def delete(entry):
        is_mcp = "entityUrn" in entry
        urn = None
        # Kill Snapshot
        if is_mcp:
            urn = entry["entityUrn"]
        else:
            snapshot_union = entry["proposedSnapshot"]
            snapshot = list(snapshot_union.values())[0]
            urn = snapshot["urn"]
        delete_urn(urn)

    with open(filename) as f:
        d = json.load(f)
        Parallel(n_jobs=10)(delayed(delete)(entry) for entry in d)

    # Deletes require 60 seconds when run between tests operating on common data, otherwise standard sync wait
    if shared_data:
        sleep(60)
    else:
        sleep(requests.ELASTICSEARCH_REFRESH_INTERVAL_SECONDS)


# Fixed now value
NOW: datetime = datetime.now()

def get_timestampmillis_at_start_of_day(relative_day_num: int) -> int:
    """
    Returns the time in milliseconds from epoch at the start of the day
    corresponding to `now + relative_day_num`

    """
    time: datetime = NOW + timedelta(days=float(relative_day_num))
    time = datetime(
        year=time.year,
        month=time.month,
        day=time.day,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return int(time.timestamp() * 1000)


def get_strftime_from_timestamp_millis(ts_millis: int) -> str:
    return datetime.fromtimestamp(ts_millis / 1000).strftime("%Y-%m-%d %H:%M:%S")


def create_datahub_step_state_aspect(
    username: str, onboarding_id: str
) -> Dict[str, Any]:
    entity_urn = f"urn:li:dataHubStepState:urn:li:corpuser:{username}-{onboarding_id}"
    print(f"Creating dataHubStepState aspect for {entity_urn}")
    return {
        "auditHeader": None,
        "entityType": "dataHubStepState",
        "entityUrn": entity_urn,
        "changeType": "UPSERT",
        "aspectName": "dataHubStepStateProperties",
        "aspect": {
            "value": f'{{"properties":{{}},"lastModified":{{"actor":"urn:li:corpuser:{username}","time":{TIME}}}}}',
            "contentType": "application/json",
        },
        "systemMetadata": None,
    }


def create_datahub_step_state_aspects(
    username: str, onboarding_ids: str, onboarding_filename
) -> None:
    """
    For a specific user, creates dataHubStepState aspects for each onboarding id in the list
    """
    aspects_dict: List[Dict[str, Any]] = [
        create_datahub_step_state_aspect(username, onboarding_id)
        for onboarding_id in onboarding_ids
    ]
    with open(onboarding_filename, "w") as f:
        json.dump(aspects_dict, f, indent=2)
