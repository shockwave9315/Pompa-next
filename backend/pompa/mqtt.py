"""paho-mqtt adapter: connect, subscribe, reconnect and forward message facts.

Transport facts live here and nowhere else:

* Subscription: ``{MQTT_TOPIC_PREFIX}/#``, QoS 0, clean session.
* LWT topic ``{prefix}/LWT`` with ``Online``/``Offline`` (MQTT-Topics.md,
  "Availability Topic"). Whether HeishaMon publishes it retained is not in the
  checked-in reference; the retain flag is forwarded and measured, not assumed.
* ``msg.retain`` is the broker's "delivered from the retained store" flag: MQTT
  3.1.1 §3.3.1.3 clears it for messages forwarded to an existing subscription.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .config import Settings
from .recorder import Recorder

log = logging.getLogger(__name__)

LWT_TOPIC = "LWT"
KEEPALIVE_SECONDS = 30
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 60


class MqttAdapter:
    def __init__(self, settings: Settings, recorder: Recorder, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.recorder = recorder
        self.clock = clock
        self.prefix = settings.mqtt_topic_prefix + "/"
        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            clean_session=True,
            protocol=mqtt.MQTTv311,
        )
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        self.client.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def start(self) -> None:
        log.info("MQTT connecting to %s:%d as %r, prefix %r", self.settings.mqtt_host,
                 self.settings.mqtt_port, self.settings.mqtt_client_id, self.settings.mqtt_topic_prefix)
        self.client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, KEEPALIVE_SECONDS)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.warning("MQTT connect refused: %s", reason_code)
            return
        self.recorder.on_connect(self.clock())
        client.subscribe(self.prefix + "#", qos=0)
        log.info("MQTT connected, epoch %d, subscribed to %s#", self.recorder.ingest.epoch, self.prefix)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.recorder.on_disconnect(self.clock())
        log.warning("MQTT disconnected: %s", reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        t = self.clock()
        try:
            if not msg.topic.startswith(self.prefix):
                return
            topic = msg.topic[len(self.prefix):]
            payload = msg.payload.decode("utf-8", errors="replace")
            if topic == LWT_TOPIC:
                self.recorder.on_lwt(payload, bool(msg.retain), t)
            else:
                self.recorder.on_message(topic, payload, bool(msg.retain), t)
        except Exception:
            log.exception("failed to handle MQTT message on %s", msg.topic)
