import json
import os
import random
import threading
import time
from datetime import datetime, timezone
import boto3

from .domain import Attribute, DataType

CREDENTIALS_PATH = os.path.abspath(os.getenv('CONFIG_CREDENTIALS_JSON', './input/config_credentials.json'))
DEFAULT_AWS_REGION = "eu-central-1"


def safe_float(value, default=0.0):
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def generate_value(config: dict, dtype: str):
    mode = config.get("mode")

    if "VECTOR" in dtype:
        if mode == "vector_custom":
            raw_vec = config.get("vector", "")
            return [float(x.strip()) if "DOUBLE" in dtype else int(float(x.strip()))
                    for x in raw_vec.split(",") if x.strip()]
        elif mode == "vector_uniform":
            raw_min = config.get("vec_min", "")
            raw_max = config.get("vec_max", "")
            min_vec = [float(x.strip()) if "DOUBLE" in dtype else int(float(x.strip()))
                       for x in raw_min.split(",") if x.strip()]
            max_vec = [float(x.strip()) if "DOUBLE" in dtype else int(float(x.strip()))
                       for x in raw_max.split(",") if x.strip()]
            result = []
            for mn, mx in zip(min_vec, max_vec):
                if "DOUBLE" in dtype:
                    result.append(round(random.uniform(mn, mx), 2))
                else:
                    result.append(random.randint(int(mn), int(mx)))
            return result

    if dtype in ("INTEGER", "DOUBLE"):
        if mode == "uniform":
            mn = safe_float(config.get("min"), 0.0)
            mx = safe_float(config.get("max"), 100.0)
            val = random.uniform(mn, mx)
            return round(val, 2) if dtype == "DOUBLE" else int(val)
        elif mode == "normal":
            mean = safe_float(config.get("mean"), 0.0)
            stddev = safe_float(config.get("stddev"), 1.0)
            val = random.gauss(mean, stddev)
            return round(val, 2) if dtype == "DOUBLE" else int(val)

    if dtype == "STRING":
        if mode == "fixed_list":
            raw_list = config.get("list", "low,medium,high")
            words = [x.strip() for x in raw_list.split(",") if x.strip()]
            return random.choice(words) if words else "low"
        elif mode == "random_string":
            return random.choice(["status_ok", "status_warn", "status_error"])

    return 0


class AWS_CREDENTIALS:
    def __init__(self, credentials_path=CREDENTIALS_PATH):
        if not os.path.exists(credentials_path):
            raise FileNotFoundError(f"Credentials file not found: {credentials_path}")
        with open(credentials_path, 'r') as f:
            self.credentials = json.load(f)


class SimulationLoop:
    def __init__(self, topic: str, region: str | None = None):
        self.topic = topic
        self.mock_aws = _is_enabled(os.getenv("SIMULATOR_MOCK_AWS"))
        self.client = None if self.mock_aws else _create_iot_client(region)
        self.active_runs: dict[str, dict] = {}
        self.sim_time = 10.0
        if self.mock_aws:
            print("SIMULATOR_MOCK_AWS is enabled. Payloads will be logged, not published to AWS IoT Core.")

    def is_running(self, attribute_id: str) -> bool:
        return self.active_runs.get(attribute_id, {}).get("run", False)

    def start_one(self, attribute: Attribute, real_device_id: str, config: dict):
        if self.is_running(attribute.id):
            return

        self.active_runs[attribute.id] = {"run": True}
        attribute.run = True

        thread = threading.Thread(
            target=self._loop,
            args=(attribute, real_device_id, config),
            daemon=True,
        )
        thread.start()

    def stop_one(self, attribute: Attribute):
        if attribute.id in self.active_runs:
            self.active_runs[attribute.id]["run"] = False
        attribute.run = False

    def _loop(self, attribute: Attribute, real_device_id: str, config: dict):
        mode = config.get("mode")
        dtype = attribute.dataType.value

        current_value = None
        mn = mx = step = None
        if mode == "range":
            mn = safe_float(config.get("min"), 0.0)
            mx = safe_float(config.get("max"), 100.0)
            step = safe_float(config.get("step"), 1.0)
            current_value = mn

        while self.active_runs.get(attribute.id, {}).get("run", False):
            if mode == "range":
                simulated_value = round(current_value, 2) if dtype == "DOUBLE" else int(current_value)
                current_value += step
                if current_value > mx:
                    current_value = mn
            else:
                simulated_value = generate_value(config, dtype)

            now_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            payload_dict = {
                "iotDeviceId": real_device_id,
                "time": now_utc,
                attribute.name: simulated_value,
            }

            print(f"Publishing to {self.topic}: {payload_dict}")
            if self.mock_aws:
                print("Mock AWS publish: 200")
                time.sleep(self.sim_time)
                continue

            try:

                response = self.client.publish(
                   topic=self.topic,
                   qos=1,
                   payload=json.dumps(payload_dict).encode('utf-8'),
                )
                print(f"AWS Response: {response.get('ResponseMetadata', {}).get('HTTPStatusCode')}")
            except Exception as e:
                print(f"Failed to publish to AWS IoT Core: {e}")

            time.sleep(self.sim_time)

        print(f"Thread stopped for: {attribute.name} ({attribute.id})")


def _create_iot_client(region: str | None):
    if os.path.exists(CREDENTIALS_PATH):
        creds = AWS_CREDENTIALS().credentials
        return boto3.client(
            'iot-data',
            region_name=region or creds["aws_region"],
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
        )

    return boto3.client(
        'iot-data',
        region_name=(
            region
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or DEFAULT_AWS_REGION
        ),
    )


def _is_enabled(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}
