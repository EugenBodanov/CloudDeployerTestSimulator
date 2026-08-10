import bisect
import json
import math
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
import boto3
from awscrt import auth, mqtt5
from awsiot import mqtt5_client_builder

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


def safe_int(value, default=0):
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def parse_headers(raw_headers: str) -> dict:
    if raw_headers is None:
        return {}

    value = str(raw_headers).strip()
    if not value:
        return {}

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}

    return parsed if isinstance(parsed, dict) else {}


def extract_json_value(payload, json_path: str):
    if json_path is None or str(json_path).strip() == "":
        return payload

    tokens = re.findall(r'[^.\[\]]+|\[\d+\]', str(json_path).strip())
    current = payload

    for token in tokens:
        if not token:
            continue

        if token.startswith('[') and token.endswith(']'):
            if not isinstance(current, list):
                return None
            index = int(token[1:-1])
            if index < 0 or index >= len(current):
                return None
            current = current[index]
        else:
            if isinstance(current, dict):
                current = current.get(token)
            else:
                return None

        if current is None:
            return None

    return current


def coerce_value(value, dtype: str):
    """Convert a value taken from an API response into the attribute's data type.

    Raises ValueError when the value cannot be represented, so the caller can skip
    the publish. Returning a fallback of 0/0.0/[] would be indistinguishable from a
    genuine reading and would hide the failure.
    """
    if value is None:
        raise ValueError(f"API value is null, cannot convert to {dtype}")

    if dtype == "STRING":
        return str(value)

    if dtype in ("INTEGER", "DOUBLE"):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"API value {value!r} is not numeric")
        try:
            number = float(value)
        except ValueError as exc:
            raise ValueError(f"API value {value!r} is not numeric") from exc
        if math.isnan(number) or math.isinf(number):
            raise ValueError(f"API value {value!r} is not a finite number")
        return int(number) if dtype == "INTEGER" else round(number, 2)

    if dtype.startswith("VECTOR_"):
        if not isinstance(value, list):
            raise ValueError(f"API value {value!r} is not a list, cannot convert to {dtype}")
        base_dtype = dtype.replace("VECTOR_", "")
        return [coerce_value(item, base_dtype) for item in value]

    return value


DEFAULT_API_PROVIDER = {
    "name": "Energy Charts renewable share forecast",
    "url": "https://api.energy-charts.info/ren_share_forecast?country=de",
    "json_path": "ren_share",
    "timestamp_path": "unix_seconds",
}


def fetch_json(url: str, headers: dict, timeout: int):
    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode('utf-8')
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"API request failed: {exc}") from exc

    try:
        return json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError("API response is not valid JSON") from exc


def select_current_sample(timestamps: list, values: list, now_epoch: float = None):
    """Pick the (timestamp, value) pair whose timestamp is the newest one at or before now.

    The provider returns a fixed local-day window rather than a rolling one, so the last
    element of the series is a forecast hours ahead, not the current reading. Sibling
    series can also be shorter than unix_seconds, so only the paired prefix is searched.
    """
    if now_epoch is None:
        now_epoch = datetime.now(timezone.utc).timestamp()

    paired = min(len(timestamps), len(values))
    if paired == 0:
        raise ValueError("API response contained no data points")

    index = bisect.bisect_right(timestamps, now_epoch, 0, paired) - 1
    index = max(0, min(index, paired - 1))
    return timestamps[index], values[index]


def fetch_predefined_api_value(dtype: str):
    """Return (value, epoch_seconds) for the current point of the predefined provider."""
    provider = DEFAULT_API_PROVIDER
    payload = fetch_json(provider["url"], {}, 15)

    values = extract_json_value(payload, provider["json_path"])
    if not isinstance(values, list) or not values:
        raise ValueError(f"API response did not contain a '{provider['json_path']}' list")

    timestamps = extract_json_value(payload, provider["timestamp_path"])
    if not isinstance(timestamps, list) or not timestamps:
        raise ValueError(f"API response did not contain a '{provider['timestamp_path']}' list")

    sample_epoch, raw_value = select_current_sample(timestamps, values)
    return coerce_value(raw_value, dtype), sample_epoch


def fetch_api_value(config: dict, dtype: str):
    """Return (value, epoch_seconds) where epoch_seconds is None if the API carries no timestamp."""
    if config.get("api_url"):
        api_url = str(config.get("api_url", "")).strip()
        json_path = config.get("json_path", "")
        timeout = max(1, safe_int(config.get("timeout"), 10))
        headers = parse_headers(config.get("headers", ""))

        payload = fetch_json(api_url, headers, timeout)

        extracted = extract_json_value(payload, json_path)
        if extracted is None:
            raise ValueError("API response field was not found")

        return coerce_value(extracted, dtype), None

    return fetch_predefined_api_value(dtype)


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
        self._lock = threading.Lock()
        self._last_generation = 0
        self.received_values: dict[tuple[str, str], dict] = {}
        self._feedback_lock = threading.Lock()
        self._mqtt5_client = None
        if self.mock_aws:
            print("SIMULATOR_MOCK_AWS is enabled. Payloads will be logged, not published to AWS IoT Core.")
        else:
            self._start_feedback_listener(region)

    def is_running(self, attribute_id: str) -> bool:
        return self.active_runs.get(attribute_id, {}).get("run", False)

    def _is_active(self, attribute_id: str, generation: int) -> bool:
        """True only for the newest run of this attribute.

        A worker can be asleep when its run is stopped and a new one started. Checking
        the run flag alone is not enough: start_one installs a fresh {"run": True}, so
        the old worker would wake up, see it, and keep publishing alongside the new one.
        """
        active_run = self.active_runs.get(attribute_id, {})
        return active_run.get("run", False) and active_run.get("generation") == generation

    def start_one(self, attribute: Attribute, real_device_id: str, config: dict):
        with self._lock:
            if self.is_running(attribute.id):
                return

            self._last_generation += 1
            generation = self._last_generation
            self.active_runs[attribute.id] = {"run": True, "generation": generation}

        attribute.run = True

        thread = threading.Thread(
            target=self._loop,
            args=(attribute, real_device_id, config, generation),
            daemon=True,
        )
        thread.start()

    def stop_one(self, attribute: Attribute):
        with self._lock:
            if attribute.id in self.active_runs:
                self.active_runs[attribute.id]["run"] = False
        attribute.run = False

    def get_feedback(self, device_id: str, property_name: str) -> dict | None:
        with self._feedback_lock:
            return self.received_values.get((device_id, property_name))

    def _start_feedback_listener(self, region: str | None):
        """Subscribe to this twin's own topic to pick up 'act*' values the pipeline
        publishes back onto it. Runs over MQTT via WebSockets with SigV4, reusing the
        same AWS credentials used for publishing - no device certificates involved.
        """
        resolved_region = _resolve_region(region)

        try:
            endpoint = _boto3_client('iot', resolved_region).describe_endpoint(
                endpointType='iot:Data-ATS'
            )['endpointAddress']

            client_id = f"simulator-{self.topic.replace('/', '-')}-{uuid.uuid4().hex[:8]}"
            credentials_provider = _resolve_credentials_provider()

            self._mqtt5_client = mqtt5_client_builder.websockets_with_default_aws_signing(
                endpoint=endpoint,
                region=resolved_region,
                credentials_provider=credentials_provider,
                client_id=client_id,
                on_publish_received=self._on_feedback_received,
                on_lifecycle_connection_success=self._on_feedback_connection_success,
            )
            self._mqtt5_client.start()
            print(f"Feedback listener connecting to {self.topic} as {client_id}")
        except Exception as exc:
            print(f"Feedback listener failed to start: {exc} - act* attributes will not receive live updates")

    def _on_feedback_connection_success(self, lifecycle_data):
        """The client defaults to a clean MQTT session (ClientSessionBehaviorType.DEFAULT),
        so the broker does not remember subscriptions across a reconnect. (Re)subscribe on
        every successful connection - not just once at startup - so a dropped connection
        doesn't silently leave this attribute deaf to feedback until the process restarts.
        """
        try:
            subscribe_future = self._mqtt5_client.subscribe(
                subscribe_packet=mqtt5.SubscribePacket(
                    subscriptions=[mqtt5.Subscription(topic_filter=self.topic, qos=mqtt5.QoS.AT_LEAST_ONCE)]
                )
            )
            subscribe_future.add_done_callback(self._on_feedback_subscribe_done)
        except Exception as exc:
            print(f"Feedback listener: failed to (re)subscribe to {self.topic}: {exc}")

    def _on_feedback_subscribe_done(self, future):
        exc = future.exception()
        if exc is not None:
            print(f"Feedback listener: subscribe to {self.topic} failed: {exc}")
        else:
            print(f"Feedback listener: subscribed to {self.topic}")

    def _on_feedback_received(self, data):
        try:
            payload = json.loads(data.publish_packet.payload.decode('utf-8'))
        except (json.JSONDecodeError, AttributeError, UnicodeDecodeError) as exc:
            print(f"Feedback listener: could not parse message payload: {exc}")
            return

        device_id = payload.get("iotDeviceId")
        source_time = payload.get("time")
        received_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

        for key, value in payload.items():
            if key in ("iotDeviceId", "time") or not key.startswith("act"):
                continue
            with self._feedback_lock:
                self.received_values[(device_id, key)] = {
                    "value": value,
                    "source_time": source_time,
                    "received_at": received_at,
                }

    def _loop(self, attribute: Attribute, real_device_id: str, config: dict, generation: int):
        mode = config.get("mode")
        dtype = attribute.dataType.value

        current_value = None
        mn = mx = step = None
        if mode == "range":
            mn = safe_float(config.get("min"), 0.0)
            mx = safe_float(config.get("max"), 100.0)
            step = safe_float(config.get("step"), 1.0)
            current_value = mn
        elif mode == "cycle":
            current_value = safe_int(config.get("start"), 0) % 24

        while self._is_active(attribute.id, generation):
            sample_epoch = None

            if mode == "range":
                simulated_value = round(current_value, 2) if dtype == "DOUBLE" else int(current_value)
                current_value += step
                if current_value > mx:
                    current_value = mn
            elif mode == "cycle":
                simulated_value = round(float(current_value), 2) if dtype == "DOUBLE" else int(current_value)
                current_value = (current_value + 1) % 24
            elif mode == "api":
                try:
                    simulated_value, sample_epoch = fetch_api_value(config, dtype)
                except Exception as exc:
                    print(f"API mode failed for {attribute.name}: {exc} - skipping publish")
                    time.sleep(self.sim_time)
                    continue
            else:
                simulated_value = generate_value(config, dtype)

            # API values carry the timestamp of the sample they were read from, so that the
            # published value and time always describe the same point in the series.
            moment = (datetime.fromtimestamp(sample_epoch, timezone.utc)
                      if sample_epoch is not None else datetime.now(timezone.utc))
            timestamp = moment.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            payload_dict = {
                "iotDeviceId": real_device_id,
                "time": timestamp,
                attribute.name: simulated_value,
            }

            # Producing the value can take a moment, so re-check that this run is
            # still the active one right before publishing.
            if not self._is_active(attribute.id, generation):
                break

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


def _resolve_credentials() -> dict | None:
    """Single source of truth for credential resolution, shared by the publish client
    and the feedback listener so the two can never end up authenticating differently.

    Returns the credentials file's contents if one is present, or None to signal
    "use this SDK's own default credential chain (env vars / IMDS / profile / etc.)".
    """
    if os.path.exists(CREDENTIALS_PATH):
        return AWS_CREDENTIALS(CREDENTIALS_PATH).credentials
    return None


def _resolve_credentials_provider() -> "auth.AwsCredentialsProvider":
    """The MQTT-side counterpart of _resolve_credentials(): builds a static
    awscrt credentials provider from the same credentials file the publish client
    uses, or falls back to awscrt's own default chain when there is no file.
    """
    creds = _resolve_credentials()
    if creds is not None:
        return auth.AwsCredentialsProvider.new_static(
            access_key_id=creds["aws_access_key_id"],
            secret_access_key=creds["aws_secret_access_key"],
            session_token=creds.get("aws_session_token"),
        )
    return auth.AwsCredentialsProvider.new_default_chain()


def _resolve_region(region: str | None) -> str:
    """Single source of truth for region resolution, shared by the publish client
    and the feedback listener so the two can never end up pointed at different regions.
    """
    if region:
        return region

    creds = _resolve_credentials()
    if creds and creds.get("aws_region"):
        return creds["aws_region"]

    return os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or DEFAULT_AWS_REGION


def _boto3_client(service_name: str, region: str | None = None):
    """Single construction point for every boto3 client in this file, so region and
    credential resolution can never drift between them - every caller (publish client,
    endpoint lookup, anything added later) goes through the same resolved values instead
    of any of them accidentally falling back to boto3's own default chain on their own.
    """
    resolved_region = _resolve_region(region)
    creds = _resolve_credentials()

    if creds is not None:
        return boto3.client(
            service_name,
            region_name=resolved_region,
            aws_access_key_id=creds["aws_access_key_id"],
            aws_secret_access_key=creds["aws_secret_access_key"],
            aws_session_token=creds.get("aws_session_token"),
        )

    return boto3.client(service_name, region_name=resolved_region)


def _create_iot_client(region: str | None):
    return _boto3_client('iot-data', region)


def _is_enabled(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}