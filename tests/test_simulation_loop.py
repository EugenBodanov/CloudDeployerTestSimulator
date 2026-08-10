import json
from unittest.mock import MagicMock

import pytest

from simulator.SimulationLoop import (
    SimulationLoop,
    _resolve_region,
    _resolve_credentials,
    _resolve_credentials_provider,
    _boto3_client,
    DEFAULT_AWS_REGION,
)


@pytest.fixture
def mock_aws_env(monkeypatch):
    monkeypatch.setenv("SIMULATOR_MOCK_AWS", "true")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


@pytest.fixture
def sim(mock_aws_env):
    """A SimulationLoop with mock_aws enabled, so construction never touches real AWS."""
    return SimulationLoop(topic="TestTwin/iot-data")


def make_message(payload: dict):
    message = MagicMock()
    message.publish_packet.payload = json.dumps(payload).encode("utf-8")
    return message


class TestResolveRegion:
    def test_explicit_region_wins_over_everything(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        assert _resolve_region("eu-west-1") == "eu-west-1"

    def test_falls_back_to_aws_region_env(self, monkeypatch):
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        assert _resolve_region(None) == "us-east-1"

    def test_falls_back_to_aws_default_region_env(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
        assert _resolve_region(None) == "ap-southeast-2"

    def test_falls_back_to_default_constant(self, monkeypatch):
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
        assert _resolve_region(None) == DEFAULT_AWS_REGION

    def test_credentials_file_region_used_when_no_explicit_region(self, monkeypatch, tmp_path):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "aws_region": "eu-central-2",
            "aws_access_key_id": "x",
            "aws_secret_access_key": "y",
        }))
        monkeypatch.setattr("simulator.SimulationLoop.CREDENTIALS_PATH", str(creds_file))
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        assert _resolve_region(None) == "eu-central-2"

    def test_explicit_region_beats_credentials_file(self, monkeypatch, tmp_path):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "aws_region": "eu-central-2",
            "aws_access_key_id": "x",
            "aws_secret_access_key": "y",
        }))
        monkeypatch.setattr("simulator.SimulationLoop.CREDENTIALS_PATH", str(creds_file))

        assert _resolve_region("sa-east-1") == "sa-east-1"

    def test_publish_client_and_feedback_listener_share_the_resolver(self, monkeypatch, tmp_path):
        """Regression test for the bug the shared resolver fixes: before this, the publish
        client and the feedback listener could silently resolve to different regions.
        """
        import simulator.SimulationLoop as sim_module

        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "aws_region": "eu-central-2",
            "aws_access_key_id": "x",
            "aws_secret_access_key": "y",
        }))
        monkeypatch.setattr(sim_module, "CREDENTIALS_PATH", str(creds_file))
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

        captured = {}
        monkeypatch.setattr(
            sim_module.boto3, "client",
            lambda *a, **kw: captured.setdefault("region", kw.get("region_name")) or MagicMock(),
        )

        sim_module._create_iot_client(None)
        publish_region = captured["region"]

        captured.clear()
        assert _resolve_region(None) == publish_region == "eu-central-2"


class TestResolveCredentials:
    def test_returns_none_when_no_credentials_file(self, monkeypatch):
        monkeypatch.setattr("simulator.SimulationLoop.CREDENTIALS_PATH", "/no/such/file.json")
        assert _resolve_credentials() is None

    def test_returns_file_contents_when_present(self, monkeypatch, tmp_path):
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "aws_region": "eu-central-2",
            "aws_access_key_id": "FILEKEY",
            "aws_secret_access_key": "FILESECRET",
        }))
        monkeypatch.setattr("simulator.SimulationLoop.CREDENTIALS_PATH", str(creds_file))

        creds = _resolve_credentials()

        assert creds["aws_access_key_id"] == "FILEKEY"
        assert creds["aws_secret_access_key"] == "FILESECRET"

    def test_provider_uses_static_credentials_matching_the_file(self, monkeypatch, tmp_path):
        """Regression test for the bug this fix addresses: before this, the MQTT feedback
        listener always used the default credential chain, ignoring the credentials file
        even when the publish client was using it - the two could authenticate differently.
        """
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "aws_region": "eu-central-2",
            "aws_access_key_id": "FILEKEY",
            "aws_secret_access_key": "FILESECRET",
        }))
        monkeypatch.setattr("simulator.SimulationLoop.CREDENTIALS_PATH", str(creds_file))

        provider = _resolve_credentials_provider()
        resolved = provider.get_credentials().result(timeout=5)

        assert resolved.access_key_id == "FILEKEY"
        assert resolved.secret_access_key == "FILESECRET"

    def test_publish_client_and_feedback_listener_share_the_same_credentials(self, monkeypatch, tmp_path):
        import simulator.SimulationLoop as sim_module

        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "aws_region": "eu-central-2",
            "aws_access_key_id": "FILEKEY",
            "aws_secret_access_key": "FILESECRET",
        }))
        monkeypatch.setattr(sim_module, "CREDENTIALS_PATH", str(creds_file))

        captured = {}
        monkeypatch.setattr(
            sim_module.boto3, "client",
            lambda *a, **kw: captured.setdefault("key", kw.get("aws_access_key_id")) or MagicMock(),
        )
        sim_module._create_iot_client(None)
        publish_key = captured["key"]

        mqtt_key = sim_module._resolve_credentials_provider().get_credentials().result(timeout=5).access_key_id

        assert publish_key == mqtt_key == "FILEKEY"

    def test_endpoint_lookup_client_also_uses_the_resolved_credentials(self, monkeypatch, tmp_path):
        """Regression test: the boto3 'iot' client used to look up the Data-ATS endpoint
        in _start_feedback_listener was built with only region_name, no credentials - so
        it silently fell back to boto3's own default chain instead of the credentials
        file, even when the publish client and MQTT provider were both using the file.
        _boto3_client() is now the single construction point for every boto3 client in
        this module, so that mistake can't happen at a new call site either.
        """
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(json.dumps({
            "aws_region": "eu-central-2",
            "aws_access_key_id": "FILEKEY",
            "aws_secret_access_key": "FILESECRET",
        }))
        monkeypatch.setattr("simulator.SimulationLoop.CREDENTIALS_PATH", str(creds_file))

        client = _boto3_client('iot', 'eu-central-2')

        assert client._request_signer._credentials.access_key == "FILEKEY"

    def test_provider_falls_back_to_default_chain_without_raising(self, monkeypatch):
        monkeypatch.setattr("simulator.SimulationLoop.CREDENTIALS_PATH", "/no/such/file.json")

        provider = _resolve_credentials_provider()  # must not raise

        assert provider is not None


class TestFeedbackReceived:
    def test_stores_only_act_prefixed_keys(self, sim):
        sim._on_feedback_received(make_message({
            "iotDeviceId": "dev-1",
            "time": "2026-08-06T10:00:00.000Z",
            "actChargeEV": 22.0,
            "someOtherReading": 5,
        }))

        feedback = sim.get_feedback("dev-1", "actChargeEV")
        assert feedback["value"] == 22.0
        assert feedback["source_time"] == "2026-08-06T10:00:00.000Z"
        assert feedback["received_at"]  # populated with a real timestamp

        assert sim.get_feedback("dev-1", "someOtherReading") is None
        assert sim.get_feedback("dev-1", "iotDeviceId") is None
        assert sim.get_feedback("dev-1", "time") is None

    def test_multiple_act_keys_stored_independently(self, sim):
        sim._on_feedback_received(make_message({
            "iotDeviceId": "dev-1",
            "time": "2026-08-06T10:00:00.000Z",
            "actChargeEV": 22.0,
            "actStopCharging": True,
        }))

        assert sim.get_feedback("dev-1", "actChargeEV")["value"] == 22.0
        assert sim.get_feedback("dev-1", "actStopCharging")["value"] is True

    def test_later_message_overwrites_earlier_value_for_same_key(self, sim):
        sim._on_feedback_received(make_message({"iotDeviceId": "dev-1", "time": "t1", "actChargeEV": 1}))
        sim._on_feedback_received(make_message({"iotDeviceId": "dev-1", "time": "t2", "actChargeEV": 2}))

        feedback = sim.get_feedback("dev-1", "actChargeEV")
        assert feedback["value"] == 2
        assert feedback["source_time"] == "t2"

    def test_different_devices_do_not_collide_on_the_same_property_name(self, sim):
        sim._on_feedback_received(make_message({"iotDeviceId": "dev-1", "time": "t1", "actChargeEV": 1}))
        sim._on_feedback_received(make_message({"iotDeviceId": "dev-2", "time": "t1", "actChargeEV": 99}))

        assert sim.get_feedback("dev-1", "actChargeEV")["value"] == 1
        assert sim.get_feedback("dev-2", "actChargeEV")["value"] == 99

    def test_malformed_payload_does_not_raise(self, sim, capsys):
        message = MagicMock()
        message.publish_packet.payload = b"not json"

        sim._on_feedback_received(message)  # must not raise

        assert "could not parse" in capsys.readouterr().out

    def test_get_feedback_returns_none_for_unknown_key(self, sim):
        assert sim.get_feedback("unknown-device", "actWhatever") is None


class TestFeedbackReconnect:
    """Covers the SDK's clean-session behavior: subscriptions aren't remembered across a
    reconnect, so the listener must resubscribe on every successful connection, not once.
    """

    def test_resubscribes_on_every_connection_success(self, sim):
        sim._mqtt5_client = MagicMock()

        sim._on_feedback_connection_success(lifecycle_data=MagicMock())
        sim._on_feedback_connection_success(lifecycle_data=MagicMock())  # simulates a reconnect

        assert sim._mqtt5_client.subscribe.call_count == 2
        for call in sim._mqtt5_client.subscribe.call_args_list:
            packet = call.kwargs["subscribe_packet"]
            assert packet.subscriptions[0].topic_filter == sim.topic

    def test_subscribe_failure_is_logged_not_raised(self, sim, capsys):
        sim._mqtt5_client = MagicMock()
        sim._mqtt5_client.subscribe.side_effect = RuntimeError("boom")

        sim._on_feedback_connection_success(lifecycle_data=MagicMock())  # must not raise

        assert "failed to (re)subscribe" in capsys.readouterr().out

    def test_subscribe_done_callback_logs_success(self, sim, capsys):
        future = MagicMock()
        future.exception.return_value = None

        sim._on_feedback_subscribe_done(future)

        assert "subscribed to" in capsys.readouterr().out

    def test_subscribe_done_callback_logs_failure(self, sim, capsys):
        future = MagicMock()
        future.exception.return_value = RuntimeError("denied")

        sim._on_feedback_subscribe_done(future)

        output = capsys.readouterr().out
        assert "subscribe to" in output and "failed" in output
